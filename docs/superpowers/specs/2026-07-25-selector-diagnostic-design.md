# Selector Diagnostic Design

## Goal

Prove whether the closed CLI can select
`6NwarBvDkXhByqVp2Qkq5i9XbtA2B3Bwe8SWGu9vpump` under a guarded,
sender-disabled profile before changing any trading filter.

Each experiment changes one primary variable while retaining the existing
workspace lock, restoration, bounded timeout, one-use authorization, secret
hygiene, no-retry policy, `0.025 SOL` early-stop, and `0.03 SOL` loss target.

## Established Evidence

- Three real single-mint runs returned zero selected mints on all `85` refreshes.
- The latest target appeared in the generated hot-token artifact with three
  pool records, LUT metadata, and recent runtime arbitrage records.
- In local historical logs, all `16` runs that ever selected a positive mint
  count also reached LUT resolution and sender dispatch. All `5` all-zero runs
  had neither.
- Existing tests prove the temporary `tokens.toml` bytes, but not the closed
  CLI's interpretation of that file.
- Official auto configuration permits `force_two_mints` to add a second active
  mint. The current profile does not require `force_two_mints = false`,
  `auto.filters.limit = 1`, or `bot.merge_mints = false`.
- Local `run --help` states that `--test-mode` runs the full pipeline without
  sending transactions to any sender.

## Non-Goals

- Do not modify the closed binary or production configuration.
- Do not change production senders, fees, tips, markets, LUT inputs, wallet
  material, or RPC sources.
- Do not loosen multiple filters in one experiment.
- Do not infer landing, execution, swaps, or PnL from discovery, construction,
  simulation, or local transport.
- Do not add automatic retries, concurrent launches, manual-market execution,
  Level 2 automation, or Level 3 optimization.

## Guarded Diagnostic Profile

The binary `run` command is never invoked directly. A new diagnostic path runs
only through `scripts/run-guarded.sh` and requires the child argv to contain:

```text
run --test-mode
```

The runner creates a private mode-600 isolated config derived from production
without printing or logging its contents. It changes only these cardinality
controls:

```text
auto.force_two_mints = false
auto.filters.limit = 1
bot.merge_mints = false
```

All selector thresholds, markets, senders, fees, tips, RPC sources, wallet
settings, loss limits, and timeout remain controlled. Production config bytes
are never edited.

The wrapper creates the exact temporary single-mint `tokens.toml`, holds the
workspace live lock, validates temporary inputs, uses existing process-group
cleanup, and restores the workspace after every outcome.

Before each diagnostic window, the coordinator displays the exact guarded argv,
command/config fingerprints, target, duration, budget, stop conditions, and
proof that `--test-mode` is present. A later exact response is single-use.

## Diagnostic Data Contract

Raw logs remain mode `600` and are never copied into chat or state files.
Secret-safe analysis records:

```json
{
  "refresh_count": 28,
  "zero_refresh_count": 28,
  "selected_count_histogram": {"0": 28},
  "selected_count_min": 0,
  "selected_count_max": 0,
  "target_artifact_present": true,
  "target_pool_count": 3,
  "target_lut_count": 153,
  "target_runtime_observation_count": 180,
  "candidate_construction": "not_observed_in_log",
  "dispatch": "not_applicable"
}
```

Any sender-dispatch line, token-account growth, loss movement, missing
`--test-mode` evidence, cleanup failure, or protected-output detection is a
fail-closed diagnostic violation.

## Experiment Decision Tree

Experiments are sequential. Each has its own exact scope and fresh approval.

### D0: Cardinality-safe baseline

Primary variable relative to the prior live run:

```text
sender-capable run -> guarded --test-mode
```

Controlled dependency: explicit target-only cardinality flags in the isolated
config.

- target selected: the target-only contract can work when cardinality is
  explicit;
- target present in discovery but never selected: continue to D1;
- target absent from discovery: stop; market evidence is insufficient.

### D1: Offchain-source filter probe

Run only if D0 has target discovery evidence and zero selection.

Primary variable:

```text
ignore_offchain_bots: isolated baseline value -> false
```

- D0 zero and D1 positive: source/offchain filtering is the supported cause;
- D0 zero and D1 zero: do not stack filters; investigate the tokens/selector
  contract or another named filter separately;
- any sender dispatch: immediate safety stop.

### D2: Contract/cardinality control

Run only if D1 remains zero and a contemporaneous known-selectable control mint
can be chosen from sanitized discovery evidence.

Primary variable:

```text
tokens cardinality: target only -> target plus one control mint
```

- control selected but target not selected: target filter rejection is
  supported;
- neither selected while a default diagnostic selects the control:
  single-mint integration is defective;
- no contemporaneous control: result remains `unknown`.

## Error Handling

- Preparation failures restore temporary inputs.
- Test-mode dispatch evidence records `test_mode_dispatch_violation` and stops.
- Cleanup failure is never success.
- Failed/interrupted diagnostics are not retried.
- A future sender-capable test requires a new exact manifest and approval after
  diagnostics are reconciled.

## Testing

All production changes use red-green-refactor.

- diagnostic child argv contains `run --test-mode` and cannot omit it;
- binary `run` remains reachable only through the guarded wrapper;
- isolated config enforces target-only cardinality controls;
- production config bytes and protected values are never emitted or modified;
- `Fetched 0`, `Fetched 1`, and `Fetched 2` fixtures produce the exact histogram,
  minimum, maximum, and zero-refresh count;
- sanitized artifacts record target, pool, LUT, and runtime counts separately
  from selection and dispatch;
- fake test-mode dispatch stops the harmless fixture with the fixed violation;
- timeout, signal, output failure, restoration, lock, and no-retry tests remain
  bounded.

Before a real diagnostic window:

```bash
python3 -m unittest tests.test_mint_runner tests.test_mint_run_shell tests.test_zavod_guard -q
bash -n scripts/mint-run.sh scripts/run-guarded.sh scripts/preflight.sh
python3 -m py_compile scripts/mint_runner.py scripts/zavod_guard.py
./zavod-mev-bot-rust-version-cli --version
./zavod-mev-bot-rust-version-cli run --help
./scripts/preflight.sh
```

Credential-bearing output is never copied into design, plan, state, or user
responses.

## Acceptance Criteria

- Existing tests remain green and new tests demonstrate red-green cycles.
- Diagnostic execution is impossible without `--test-mode`, guarded wrapper,
  workspace lock, restoration, bounded timeout, and one-use approval.
- Production config and binary hashes remain unchanged.
- D0 produces an interpretable selector result without sender dispatch.
- Later experiments change one primary variable and receive fresh approval.
