#!/usr/bin/env python3
import argparse
import codecs
import json
import os
import re
import shutil
import signal
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


def validate_config(config, profile="default", root=None):
    errors = []
    expected_senders = dict(PROFILE_SENDERS.get(profile, EXPECTED_SENDERS))
    if profile == "ab-no-swqos":
        expected_senders["helius_swqos"] = False
    elif profile not in (
        "default",
        "manual-single",
        "single-mint-auto",
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
        if profile == "single-mint-auto":
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
    def __init__(self, sink, policy):
        self.sink = sink
        self.policy = (
            policy
            if isinstance(policy, ProtectedOutputPolicy)
            else ProtectedOutputPolicy(policy)
        )
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
                self.sink.write("<redacted>")
                self.buffer = self.buffer[len(secret):]
                return True

        url_scheme_match = self.policy.URL_SCHEME_PATTERN.match(self.buffer)
        if url_scheme_match is not None:
            self.sink.write("<redacted>")
            self.buffer = self.buffer[url_scheme_match.end():]
            self._discard_url = True
            return True

        uuid_match = self.policy.UUID_PATTERN.match(self.buffer)
        if uuid_match is not None:
            self.sink.write("<redacted>")
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
                        self.sink.write("<redacted>")
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
    def __init__(self, source, sink, config):
        self.source = source
        self.redactor = StreamingRedactor(
            sink,
            ProtectedOutputPolicy.from_config(config),
        )
        self.output_error_event = threading.Event()
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
    killpg=os.killpg,
    group_exists=None,
    signal_grace=DEFAULT_SIGNAL_GRACE,
    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    cleanup_child=True,
    operator_signal_event=None,
):
    end_balance = start_balance
    reason = None
    exit_code = None
    try:
        started_at = monotonic()
        while reason is None:
            if (
                operator_signal_event is not None
                and operator_signal_event.is_set()
            ):
                reason = "operator_signal"
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
            if should_stop_for_loss(start_balance, end_balance):
                reason = "loss_threshold"
                break
            sleep(1)
    except KeyboardInterrupt:
        reason = "operator_signal"
    finally:
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
            elif cleanup["interrupted"] and reason != "output_error":
                reason = "operator_signal"
    return {
        "reason": reason,
        "start_balance": start_balance,
        "end_balance": end_balance,
        "observed_loss": max(0, start_balance - end_balance),
        "child_exit_code": exit_code,
    }


def _cli_version(binary):
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuardError("CLI version check failed") from exc
    match = re.search(r"zavod-mev-bot-rust-version-cli\s+([0-9]+\.[0-9]+\.[0-9]+)", result.stdout + result.stderr)
    if not match:
        raise GuardError("CLI version check failed")
    return match.group(1)


def preflight(
    config_path,
    root=None,
    config=None,
    pubkey_resolver=wallet_pubkey,
    balance_reader=get_balance_lamports,
    disk_free_reader=None,
    profile="default",
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
    binary = root / "zavod-mev-bot-rust-version-cli"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise GuardError("Zavod CLI is missing or not executable")
    version = _cli_version(binary)
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


def run_guarded(config_path, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, profile="default"):
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 30 <= timeout_seconds <= DEFAULT_TIMEOUT_SECONDS
    ):
        raise GuardError("timeout must be from 30 through 300 seconds")
    config_path = Path(config_path).resolve()
    root = config_path.parent
    config = load_config(config_path)
    summary = preflight(config_path, root=root, config=config, profile=profile)
    public_key = summary["wallet"]
    rpc_url = _get(config, "rpc", "url")
    start_balance = summary["balance_lamports"]
    logs_dir = root / "logs"
    logs_dir.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = logs_dir / f"{stamp}-zavod-cli.log"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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

    def interrupt_handler(signum, frame):
        del signum, frame
        operator_signal_event.set()

    try:
        log_handle = os.fdopen(fd, "w", buffering=1)
        signal.signal(signal.SIGINT, interrupt_handler)
        signal.signal(signal.SIGTERM, interrupt_handler)
        child = subprocess.Popen(
            [str(root / "zavod-mev-bot-rust-version-cli"), "run"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if operator_signal_event.is_set():
            result = {
                "reason": "operator_signal",
                "start_balance": start_balance,
                "end_balance": start_balance,
                "observed_loss": 0,
                "child_exit_code": None,
            }
        else:
            pump = OutputPump(child.stdout, log_handle, config)
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
                    start_balance=start_balance,
                    balance_reader=lambda: get_balance_lamports(
                        rpc_url,
                        public_key,
                    ),
                    monotonic=time.monotonic,
                    sleep=time.sleep,
                    output_error_event=pump.output_error_event,
                    timeout_seconds=timeout_seconds,
                    cleanup_child=False,
                    operator_signal_event=operator_signal_event,
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
            log_path.chmod(0o600)
        finally:
            try:
                _, interrupted = _retry_keyboard_interrupt(
                    lambda: signal.signal(signal.SIGINT, prior_sigint)
                )
                finalization_interrupted |= interrupted
                _, interrupted = _retry_keyboard_interrupt(
                    lambda: signal.signal(signal.SIGTERM, prior_sigterm)
                )
                finalization_interrupted |= interrupted
            except GuardError:
                finalization_interrupted = True
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
    elif pump is not None and (
        pump.output_error_event.is_set()
        or (pump_started and pump.is_alive())
    ):
        result["reason"] = "output_error"
    elif (
        operator_signal_event.is_set()
        or cleanup["interrupted"]
        or finalization_interrupted
    ):
        result["reason"] = "operator_signal"
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    result["log_path"] = str(log_path.relative_to(root))
    result["loss_limit_lamports"] = LOSS_LIMIT_LAMPORTS
    result["early_stop_lamports"] = EARLY_STOP_LAMPORTS
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Secret-safe Zavod operations guard")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--config", default="config.toml")
    preflight_parser.add_argument("--profile", default="default")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--config", default="config.toml")
    run_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    run_parser.add_argument("--profile", default="default")
    run_parser.add_argument("--live-confirmed", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            _print_summary(preflight(args.config, profile=args.profile))
            return 0
        if args.command == "run":
            if not args.live_confirmed:
                raise GuardError("live confirmation is required")
            _print_run_result(run_guarded(args.config, args.timeout_seconds, args.profile))
            return 0
    except GuardError as exc:
        print(f"status=failed\nerror={exc}", file=os.sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
