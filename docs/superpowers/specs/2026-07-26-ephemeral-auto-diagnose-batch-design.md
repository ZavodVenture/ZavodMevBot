# Ephemeral Auto-Diagnose Batch Design

## Goal

Finish the autonomous mint filter diagnosis as one bounded, non-resumable
process. Preserve the live-lock and cumulative-loss protections already built,
while removing the crash-recovery state machine that made Task 3 unsafe and
unnecessarily large.

## Scope

This design supersedes Task 3's persistent stage state, pending-result
recovery, quarantine, and automatic resume behavior. It does not change the
approved private stage preparation or the descriptor-bound
`auto-filter-live` guard.

## Execution Model

`mint-auto-diagnose.sh` owns the production live lock for the entire batch. It
prepares private stage workspaces once, then runs each non-skipped stage in
declared order for at most 300 seconds through `scripts/run-guarded.sh`.

There are at most eight stages and no stage retry. Stage decisions exist only
in the orchestrator process:

- `target_positive`: stop successfully;
- `continue`: execute the next declared non-skipped stage;
- `failed`: stop the batch immediately.

The batch is never resumed. A signal, process crash, restart, ambiguous
cleanup, malformed evidence, RPC failure, or missing expected artifact makes
that batch failed. A later operator request creates a new batch.

## Evidence

After each completed stage, the diagnoser reads each relevant log or artifact
once through `O_NOFOLLOW` descriptors into bounded immutable snapshots. Logs
are capped at 8 MiB. Structured artifacts use small fixed caps appropriate to
their schemas. Metadata and the named inode are revalidated after the read.

Target-positive requires either:

- the exact target mint in a structurally valid selector/routing snapshot; or
- at least one finalized target-filtered landed transaction from the existing
  read-only chain aggregation.

Unrelated sender acceptance is diagnostic only. Explicit route length three
sets `three_hop_observed`; otherwise three-hop remains `unproven`.

Any malformed, stale, oversized, substituted, symlinked, or wrong-mode
evidence stops the batch as failed. Unknown routing schemas fail closed and do
not create target-positive evidence.

## Loss and Stop Policy

All stages use the one immutable wallet baseline recorded during preparation.
The guard refuses or stops execution at a cumulative loss of 25,000,000
lamports. The batch hard loss target remains 30,000,000 lamports. A threshold
event, nonzero child result, protected-output event, cleanup failure, or RPC
failure stops the batch without retry.

## Output and Cleanup

During the run, decisions remain in memory. Each completed stage may write one
mode-600 sanitized stage summary for operator inspection, but no summary is
used to resume execution.

At termination, the orchestrator makes one best-effort atomic mode-600
`batch-result.json` containing only sanitized public counters, the terminal
status, stop reason, executed stage names, target/three-hop status, and public
loss limits. Failure to publish this file does not authorize retry or resume.

The orchestrator always attempts idempotent cleanup and active-marker removal
while still holding the live lock. Cleanup ambiguity is reported as failure.
No quarantine or recovery journal is used.

## Minimal Verification

Tests cover only the load-bearing behavior:

1. exact stage order with no retries;
2. target-positive stops immediately;
3. missing target advances once;
4. cumulative 25,000,000-lamport stop;
5. malformed or substituted evidence fails closed;
6. interruption never resumes a stage;
7. sender acceptance alone never succeeds;
8. the shell invokes only `scripts/run-guarded.sh`.

The existing guard and preparation suites remain unchanged and must continue
to pass. No live or transaction-capable command is part of implementation
verification.
