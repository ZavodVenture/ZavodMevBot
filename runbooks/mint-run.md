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

## D0 selector diagnostic

D0 is a test-mode, sender-dispatch-forbidden, single-target selector
observation. It is a separate operator route:

```bash
./scripts/mint-run.sh <MINT> --diagnostic d0 --timeout 300
```

Do not call `run-guarded.sh`, `zavod_guard.py run`, or the binary directly.
The wrapper prepares a run-bound private diagnostic config, holds the existing
live lock, and binds the prepared launch contract to:

- diagnostic mode `d0`;
- the exact target mint;
- the prepared config and `tokens.toml` SHA-256 identities.

The wrapper and guard additionally enforce profile `selector-diagnostic`, one
and only one `--test-mode`, and the requested bounded timeout. The normal
thresholds remain `0.025 SOL` early-stop and `0.03 SOL` loss target and are
reported unchanged, but D0 fails closed on any positive SOL loss.

Only D0 is launchable by this route. D1 and D2 are separate experiments and
require separate design review and fresh approval.

### Approval boundary

Before invoking the wrapper, present a secret-safe approval manifest containing
the target, duration, reviewed/deployed commit identity, withheld-but-verified
config and binary fingerprints, exact top-level command, internal guarded
semantics, stop conditions, artifact locations, one-attempt/no-retry policy,
and the exact confirmation phrase.

The manifest, reviewed/deployed commit identity, binary/config fingerprints,
and approval freshness are external controller gates; the child process does
not independently validate those operator records.

Obtain fresh explicit approval immediately before invocation. For a 300-second
run, the wrapper accepts exactly one line:

```text
DIAGNOSE <MINT> FOR 300
```

Additional stdin is not a second approval. A decline, malformed confirmation,
violation, interruption, cleanup failure, or ambiguous result ends the attempt;
do not retry automatically.

### Fail-closed conditions

Any of the following makes D0 unsafe/invalid and makes the top-level command
exit nonzero: dispatch evidence, any positive SOL loss, token-account growth,
RPC failure, protected-output detection, config/tokens identity change, invalid
mint account, stale generated-artifact destination, process/lock violation, or
cleanup failure. Depending on the failure point, no diagnostic manifest may be
produced.

The guard stops the process group; the wrapper/finalizer lifecycle restores the
original config, token list, and runtime artifacts. After the attempt, the
operator/controller must independently prove the process group is absent, the
live lock is free, the active marker and diagnostic config are absent, and
protected inputs retain their original bytes and mode.

### D0 evidence

Use only the fixed `selector_diagnostic` manifest fields:

- refresh and zero-refresh counts;
- selected-count histogram and min/max;
- target artifact presence and target pool/LUT/runtime-observation counts;
- candidate-construction marker;
- dispatch status.

Full diagnostic routing/hot-token artifacts are sanitized and parsed only in
memory and are not retained. Chain landing, execution, swaps, fees, and PnL are
`not_applicable` in D0 and must not be used to infer selector behavior.

Interpretation:

- target absent from valid artifact evidence: stop; discovery evidence is absent;
- target present with selector counts always zero: discovery exists but selection
  did not admit the mint; propose D1 separately, without retrying D0;
- positive target selection: the target-only selector contract is supported;
- any fail-closed condition or unavailable evidence: no selector conclusion.

Update both `state/CURRENT.md` and `state/EXPERIMENTS.md` after every material
check or D0 attempt, including restoration proof and the no-retry outcome.
