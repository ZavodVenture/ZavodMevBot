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

Pool discovery, routing, and LUT resolution remain inside ZavodMevBot auto mode.
No static `markets.toml` or new on-chain LUT is created.

After timeout, stop, failure, or Ctrl-C, the original files are restored. Results
are written under `state/mint-runs/<timestamp>/`; recovery copies remain under
`state/backups/mint-run-<timestamp>/`.

## Safety

- Never call the binary's `run` command directly.
- Never reuse an old confirmation.
- There is no automatic retry.
- Early-stop is `0.025 SOL`; loss target is `0.03 SOL`.
- A preparation error means no transaction-capable command was executed.
