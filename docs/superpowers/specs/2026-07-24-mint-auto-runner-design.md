# Mint Auto Runner Design

## Goal

Provide one operator command:

```bash
./scripts/mint-run.sh <MINT> [--timeout SECONDS]
```

The command temporarily restricts ZavodMevBot auto mode to the supplied Solana token mint, validates readiness, requests explicit confirmation immediately before live execution, runs one guarded window that defaults to 300 seconds, restores the original workspace configuration, and records a secret-safe result.

## Scope

The first version:

- accepts exactly one Solana token mint;
- permits trading only for that target mint while allowing the bot to use quote assets internally;
- relies on ZavodMevBot `0.2.2` auto mode for pool discovery, routing, and LUT resolution;
- does not generate or activate a static `markets.toml`;
- does not create a new on-chain lookup table;
- performs exactly one live attempt after interactive confirmation;
- defaults to 300 seconds and accepts an optional timeout from 30 through 300 seconds;
- restores the original workspace files after success, failure, timeout, or operator interruption.

## User Interface

The entry point is:

```bash
./scripts/mint-run.sh <MINT> [--timeout SECONDS]
```

The script performs all read-only and local preparation automatically. Before calling the transaction-capable runner, it prints a secret-safe summary containing:

- the target mint;
- CLI version;
- timeout;
- early-stop threshold;
- loss target;
- confirmation that auto mode and preflight passed.

The operator must enter the exact phrase `RUN <MINT> FOR <SECONDS>` immediately before execution, with the displayed mint and timeout substituted literally. Any other input exits without starting the bot and restores the original files.

## Architecture

### `scripts/mint-run.sh`

This is the only operator-facing command. It:

- resolves the workspace root;
- rejects malformed arguments and timeout values outside 30 through 300 seconds;
- refuses to continue if another ZavodMevBot process is active;
- delegates preparation and validation to `mint_runner.py`;
- displays the execution summary and reads the exact confirmation phrase;
- invokes `./scripts/run-guarded.sh --live-confirmed --timeout <SECONDS>`;
- always invokes restoration and result finalization.

### `scripts/mint_runner.py`

This module owns local state transitions and read-only diagnostics. It:

- validates the mint as a base58-encoded 32-byte public key;
- queries the configured RPC to confirm the account exists, is non-executable, and is owned by the SPL Token or Token-2022 program;
- validates `config.toml` without printing its values;
- requires mode `600`, `[auto].enabled = true`, and CLI version `0.2.2`;
- creates a timestamped mode-700 recovery directory under `state/backups/`;
- creates mode-600 recovery copies of `config.toml`, `tokens.toml`, and any existing `hot_tokens.json` and `routing.json` inside that recovery directory;
- creates a separate mode-700 result directory under `state/mint-runs/`;
- writes a temporary `tokens.toml` containing only the supplied mint;
- removes stale runtime routing artifacts before preflight so a previous mint cannot affect discovery;
- runs the read-only guarded preflight;
- restores original files in an idempotent finalizer;
- writes a secret-safe run manifest and aggregate result.

It never invokes the bot binary with `run`.

### `scripts/run-guarded.sh`

This remains the only allowed transaction-capable launch path. It:

- requires `--live-confirmed`;
- passes the validated timeout, defaulting to 300 seconds and never exceeding it;
- invokes `zavod_guard.py`;
- accepts no automatic retry option.

### `scripts/zavod_guard.py`

The supervisor:

- validates the CLI, config, RPC, wallet balance, disk space, and sender settings;
- starts the CLI in a new process group;
- enforces the `0.025 SOL` early-stop threshold and `0.03 SOL` loss target;
- stops fail-closed on RPC monitoring failure;
- handles timeout and operator signals;
- escalates process-group shutdown through `SIGINT`, `SIGTERM`, and `SIGKILL`;
- verifies that the complete process group is gone;
- writes a mode-600 redacted log.

It does not implement a WSOL-specific stop condition.

## Data Flow

1. Validate the mint locally.
2. Confirm through RPC that the mint is a valid token mint.
3. Confirm no ZavodMevBot process is active.
4. Create the timestamped recovery snapshot under `state/backups/`.
5. Replace `tokens.toml` with a single-mint file.
6. Move stale `hot_tokens.json` and `routing.json` into the recovery snapshot.
7. Run read-only preflight.
8. Display the execution summary.
9. Request the exact interactive confirmation phrase.
10. Invoke `run-guarded.sh` once.
11. Aggregate the log and finalized on-chain activity for the exact run window.
12. Restore the original files.
13. Update `state/CURRENT.md` and `state/EXPERIMENTS.md`.

## Pool and LUT Behavior

The runner does not duplicate DEX discovery logic.

With the temporary single-mint `tokens.toml` and `[auto].enabled = true`, the bot performs its own mint refresh, pool selection, routing, and LUT resolution. Static `markets.toml` generation is intentionally excluded from the first version.

If the bot cannot resolve pools or LUTs during the live window, the runner records the absence of discovery or transactions. It does not create a new lookup table, retry automatically, or broaden the token set.

## Recovery and Failure Handling

Preparation is fail-closed. No live command is allowed when:

- the mint is malformed or is not a token mint;
- another bot process is active;
- `config.toml` is invalid or has unsafe permissions;
- auto mode is disabled;
- the CLI version differs from `0.2.2`;
- RPC validation or preflight fails;
- the recovery snapshot cannot be created;
- the temporary single-mint file fails structural validation;
- explicit confirmation is not provided.

Restoration is idempotent and runs after:

- declined confirmation;
- normal timeout;
- early-stop;
- RPC failure;
- child-process failure;
- Ctrl-C or SIGTERM;
- aggregation failure.

Restoration never replaces a recovery snapshot with generated runtime output. Generated output is copied into the run record before the original files are restored.

## Run Records

Each attempt receives a timestamped mode-700 directory under:

```text
state/mint-runs/<timestamp>/
```

The record may contain:

- a secret-safe manifest;
- the target mint;
- preparation and stop reason;
- pool/LUT/log event counts;
- landed, successful, and failed transaction counts;
- aggregate fees, rent, transfers, SOL delta, and wSOL delta;
- generated `hot_tokens.json` and `routing.json` when present.

It must not contain:

- `config.toml`;
- wallet material;
- authenticated RPC URLs;
- API keys;
- unredacted CLI output;
- transaction signatures or UUIDs.

Recovery copies remain in `state/backups/` with mode `600` and are not included in the public run manifest.

## Testing

All automated tests use a fake CLI and mock RPC transport. They create no transactions.

Tests cover:

- valid and invalid base58 mint inputs;
- existing non-token accounts and valid Token/Token-2022 mint accounts;
- invalid TOML and unsafe config permissions;
- disabled auto mode;
- wrong CLI version;
- an already-running bot process;
- snapshot creation and mode enforcement;
- exact single-mint `tokens.toml`;
- stale runtime-artifact isolation;
- preflight failure;
- declined or incorrect confirmation;
- one successful guarded invocation;
- timeout, early-stop, RPC failure, Ctrl-C, and child failure;
- process-group cleanup including surviving descendants;
- idempotent restoration after every failure boundary;
- secret redaction across output chunks;
- manifest aggregation without signatures, UUIDs, URLs, or secret values;
- absence of automatic retry.

## Acceptance Criteria

The feature is accepted when:

- the operator can invoke one command with one mint;
- no live execution occurs without the exact immediate confirmation;
- the bot sees only the target mint from `tokens.toml`;
- auto mode is responsible for pools, routing, and LUTs;
- one guarded window runs for the selected 30–300 seconds plus bounded shutdown time;
- financial stop controls remain unchanged;
- the original workspace files are restored byte-for-byte;
- the bot process group is absent after every exit path;
- the log and run manifest use restricted permissions and contain no protected values;
- state documentation is updated after preparation failures and material runs.
