# Mint Auto-Diagnose Runbook

## Purpose

Run one bounded, non-resumable live selector diagnosis for a single mint. The
batch relaxes up to eight configured filters in order until exact target
evidence appears or a safety condition stops execution.

## Command

```bash
./scripts/mint-auto-diagnose.sh GiRrLzdan5Gz31ngH4zgxk6ybYaryNVCSLdAJyn1pump
```

The script requires this exact single-use confirmation:

```text
AUTODIAGNOSE GiRrLzdan5Gz31ngH4zgxk6ybYaryNVCSLdAJyn1pump WITH 0.03 SOL
```

Do not provide the confirmation until production preflight is complete.

## Fixed Safety Contract

- One production live lock is held for the entire batch.
- Every stage runs only through `scripts/run-guarded.sh`.
- Each stage is limited to 300 seconds.
- There are at most eight stages and no retry.
- Three-hop remains enabled in every executed stage.
- Cumulative early stop is 0.025 SOL (25,000,000 lamports).
- The hard loss target is 0.03 SOL (30,000,000 lamports).
- The baseline is sampled once during preparation and shared by every stage.

## Stage Order

Stages already at their permissive value are skipped:

1. `baseline`
2. `offchain`
3. `activity`
4. `aggregate_profit`
5. `per_arb_profit`
6. `roi`
7. `volume`
8. `pool_liquidity`

## Result Rules

`target_positive` requires the exact target mint in a structurally valid
selector/routing artifact or at least one finalized target-filtered landed
transaction. Sender acceptance alone is never success.

An explicit target route with three pool IDs records `three_hop_status` as
`observed`. Absence of that exact structure records `unproven`.

Malformed, stale, oversized, substituted, symlinked, or wrong-mode evidence
fails the batch. RPC failure, nonzero guard exit, cleanup ambiguity, protected
output, cumulative threshold, signal, or process interruption also fails the
batch.

## No Resume

The process keeps stage decisions only in memory. A failed or interrupted
batch is never resumed and no stage is automatically retried. A later attempt
requires a new batch and a new exact confirmation.

## Artifacts

Private stage workspaces:

```text
state/auto-diagnose-runs/BATCH_ID/stages/INDEX-NAME/
```

Best-effort terminal report:

```text
state/auto-diagnose-runs/BATCH_ID/batch-result.json
```

The terminal report is mode 600 and reporting-only. It is never consumed to
authorize or resume execution.

## Cleanup

The shell keeps the live lock while attempting idempotent marker cleanup. If
normal cleanup is interrupted, use the read-only-safe recovery command before
any new batch:

```bash
python3 scripts/mint_auto_diagnoser.py restore-active /opt/zavod
```

Treat cleanup ambiguity as a failed batch. Never start another live run until
the active marker and live-lock ownership have been checked.
