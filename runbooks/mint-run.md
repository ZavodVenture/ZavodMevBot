# Single-mint guarded run

## Command

```bash
./scripts/mint-run.sh <MINT> [--timeout SECONDS]
```

`SECONDS` defaults to `300` and must be from `30` through `300`.

## Behavior

The command validates the token mint, snapshots the active workspace, temporarily
places only the requested mint in `tokens.toml`, runs guarded preflight, and prints
the exact confirmation phrase. The bot starts only after that phrase is entered.
Preparation and the final live preflight both require auto mode with every
`[[markets_file]]` source explicitly disabled.

Pool discovery, routing, and LUT resolution remain inside ZavodMevBot auto mode.
No static `markets.toml` or new on-chain LUT is created.

After timeout, stop, failure, or Ctrl-C, the original files are restored. Results
are written under `state/mint-runs/<timestamp>/`; recovery copies remain under
`state/backups/mint-run-<timestamp>/`.

A workspace advisory lock is held from before preparation through finalization
and restoration. Concurrent mint runs and direct guarded live runs fail before
the bot can start. Generated runtime artifacts are preserved only as structurally
valid, sanitized JSON.

## Safety

- Never call the binary's `run` command directly.
- Never reuse an old confirmation.
- There is no automatic retry.
- Early-stop is `0.025 SOL`; loss target is `0.03 SOL`.
- A preparation error means no transaction-capable command was executed.
- Environment-backed RPC and wallet settings are supported without being copied
  into logs or run records.
