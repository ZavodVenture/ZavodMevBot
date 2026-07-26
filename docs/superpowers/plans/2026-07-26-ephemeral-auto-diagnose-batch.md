# Ephemeral Auto-Diagnose Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unsafe persistent Task 3 state machine with a single-process, non-resumable stage evaluator and finish the bounded autonomous shell loop.

**Architecture:** Keep the approved private stage preparation and `auto-filter-live` guard. Evaluate one completed stage from bounded descriptor snapshots, return one of three in-memory decisions, and let one shell process own the live lock and stage order for the entire batch. No persistent state transition, pending directory, quarantine, recovery, or automatic resume remains.

**Tech Stack:** Python 3.12 standard library, Bash, `unittest`, existing `mint_runner.py` sanitizers and chain aggregation.

## Global Constraints

- Never print or persist configuration secrets, authenticated RPC URLs, wallet material, UUIDs, API keys, protected fingerprints, or transaction signatures.
- Never invoke the binary's `run` command directly; every stage passes through `scripts/run-guarded.sh`.
- Production `config.toml`, `tokens.toml`, and binary bytes remain unchanged.
- Private directories are mode 700 and private files are mode 600.
- One exact confirmation authorizes one batch; stages do not prompt again.
- Cumulative early stop is 25,000,000 lamports and hard loss target is 30,000,000 lamports from one batch baseline.
- Each stage lasts at most 300 seconds; there are no retries and at most eight stages.
- Three-hop remains enabled in every stage.
- A signal, crash, restart, ambiguous cleanup, malformed evidence, RPC failure, threshold, or nonzero child result terminates the batch without resume.
- No transaction-capable command is used during implementation verification.

---

### Task 1: Replace persistent evidence state with one-shot evaluation

**Files:**
- Modify: `scripts/mint_auto_diagnoser.py`
- Modify: `tests/test_mint_auto_diagnoser.py`

**Interfaces:**
- Preserve: `prepare_batch(root, mint, now=None, transport=None, balance_reader=None) -> PreparedBatch`.
- Preserve: `restore_batch(root, batch_id) -> None`.
- Produce: `evaluate_stage(root: Path, batch_id: str, stage_name: str, guard_exit: int, started_at: int, ended_at: int, transport=None) -> dict`.
- Produce CLI: `evaluate-stage`.
- Remove: `record_stage_result`, `next_stage`, `finalize_batch`, `record-stage`, `next-stage`, `finalize`, persistent `batch-state.json`, pending-result recovery, quarantine, and state-transition locks.

- [ ] **Step 1: Replace recovery tests with seven failing one-shot tests**

Create focused tests named:

```python
test_exact_target_returns_target_positive
test_sender_acceptance_alone_returns_continue
test_missing_target_returns_continue
test_nonzero_guard_exit_returns_failed
test_threshold_or_rpc_error_returns_failed
test_malformed_or_substituted_evidence_returns_failed
test_interrupted_batch_has_no_resume_interface
```

Each test uses private fixture workspaces only. Assert the returned dictionary
contains the fixed public fields:

```python
{
    "stage_name": str,
    "decision": "target_positive" | "continue" | "failed",
    "stop_reason": str,
    "target_status": "positive" | "absent" | "unproven",
    "three_hop_status": "observed" | "unproven",
    "sender_accepted": int,
    "sender_rejected": int,
    "target_landed": int,
    "cumulative_loss_lamports": int,
}
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python3 -m unittest -v tests.test_mint_auto_diagnoser.StageEvidenceTests
```

Expected: failures because `evaluate_stage` is absent and recovery interfaces
still exist.

- [ ] **Step 3: Delete the persistent state/recovery implementation**

Remove all Task 3 code for state transitions, pending result directories,
quarantine, finalization recovery, and resume-oriented CLI commands. Preserve
Task 1 preparation/restoration and their state-marker locking behavior.

The module must no longer create or consume:

```text
batch-state.json
.pending-*
stage-manifest.json as execution state
```

- [ ] **Step 4: Implement bounded one-shot evidence evaluation**

Read the stage log once with `O_NOFOLLOW` into an immutable snapshot capped at
8 MiB. Read each structured artifact once with `O_NOFOLLOW` into an immutable
snapshot capped at 1 MiB. For every snapshot, validate regular-file type,
owner, mode 600, stage time bucket, unchanged device/inode/size/mtime after
read, and current path identity.

Reuse the existing exact hot-token validator, protected JSON sanitizer,
bounded log parsing, and finalized target-filtered chain aggregation.
Unknown routing schemas return `failed/artifact_error`.

Decision order is fixed:

```python
if guard_exit != 0 or safety_error or cumulative_loss >= 25_000_000:
    decision = "failed"
elif exact_structural_target or target_landed > 0:
    decision = "target_positive"
else:
    decision = "continue"
```

No function writes execution state or authorizes a later stage.

- [ ] **Step 5: Make `evaluate-stage` print one sanitized JSON object**

The CLI accepts only batch ID, stage name, signed guard exit, and integer start
and end times. It loads private contract values from the prepared workspace.
It prints exactly the fixed public result object and never a signature, URL,
wallet identifier, config value, balance, or digest.

- [ ] **Step 6: Run Task 1 verification**

Run:

```bash
python3 -m unittest -v tests.test_mint_auto_diagnoser
python3 -m py_compile scripts/mint_auto_diagnoser.py
git diff --check
```

Expected: all diagnoser tests pass and the module is materially smaller than
the current 2,557-line implementation.

### Task 2: Implement the non-resumable guarded shell loop

**Files:**
- Create: `scripts/mint-auto-diagnose.sh`
- Create: `tests/test_mint_auto_diagnose_shell.py`
- Modify: `scripts/mint_auto_diagnoser.py`

**Interfaces:**
- Consumes: `prepare`, `stage-contract-path`, `evaluate-stage`, and `restore`.
- Consumes: `scripts/run-guarded.sh --profile auto-filter-live`.
- Produces: one terminal sanitized `batch-result.json` and shell exit status.

- [ ] **Step 1: Write six failing shell tests**

Cover:

```text
exact confirmation required once
declared non-skipped stages execute in order
target_positive stops immediately
continue advances exactly once with no retry
failed or signal stops and restores
only scripts/run-guarded.sh launches a stage
```

Use fake guard/evaluator commands and private fixture roots. No real binary,
wallet, RPC, or sender is used.

- [ ] **Step 2: Run shell tests and verify RED**

Run:

```bash
python3 -m unittest -v tests.test_mint_auto_diagnose_shell
```

Expected: failure because `scripts/mint-auto-diagnose.sh` is absent.

- [ ] **Step 3: Implement the linear loop**

The script:

1. validates the mint and exact confirmation;
2. acquires the production live lock once;
3. calls `prepare` once;
4. iterates the prepared non-skipped stages once in declared order;
5. opens the stage contract and passes inherited contract/live-lock FDs to
   `scripts/run-guarded.sh --profile auto-filter-live`;
6. calls `evaluate-stage` once for that stage;
7. stops on `target_positive` or `failed`, otherwise advances;
8. never re-enters a stage;
9. traps `EXIT`, `INT`, and `TERM` to call idempotent restore while holding the
   live lock.

- [ ] **Step 4: Write one best-effort terminal result**

Add `write-batch-result` to `mint_auto_diagnoser.py`. It atomically writes a
mode-600 sanitized result containing:

```text
batch_id
target_mint
terminal_status
stop_reason
executed_stage_names
target_status
three_hop_status
cumulative_early_stop_lamports
cumulative_loss_limit_lamports
```

This file is reporting only. It is never read to resume or authorize work.
Publication failure changes the shell outcome to failed but never retries.

- [ ] **Step 5: Run Task 2 verification**

Run:

```bash
python3 -m unittest -v tests.test_mint_auto_diagnose_shell
bash -n scripts/mint-auto-diagnose.sh
python3 -m py_compile scripts/mint_auto_diagnoser.py
git diff --check
```

Expected: all focused tests and syntax checks pass.

### Task 3: Transaction-free integration gate

**Files:**
- Create: `runbooks/mint-auto-diagnose.md`
- Modify: `docs/superpowers/specs/2026-07-26-ephemeral-auto-diagnose-batch-design.md`
- Modify: `docs/superpowers/plans/2026-07-26-ephemeral-auto-diagnose-batch.md`

**Interfaces:**
- Produces: reviewed implementation and exact production-preflight procedure.

- [ ] **Step 1: Document the operator contract**

Document the exact command and confirmation, eight-stage maximum, 300-second
stage timeout, cumulative 0.025/0.03 SOL limits, no retry/resume behavior,
target-positive rule, artifact locations, and cleanup procedure.

- [ ] **Step 2: Run one fresh full verification**

Run:

```bash
python3 -m unittest discover -v
python3 -m py_compile scripts/zavod_guard.py scripts/mint_auto_diagnoser.py
bash -n scripts/run-guarded.sh scripts/mint-auto-diagnose.sh
git diff --check
```

Expected: all transaction-free tests pass.

- [ ] **Step 3: Review the final branch diff once**

Review only for Critical or Important violations of:

```text
no direct binary run
one live lock for the batch
no retries or resume
25,000,000 cumulative early stop
30,000,000 hard target
target evidence independent from sender acceptance
protected-output safety
```

- [ ] **Step 4: Stop before production execution**

After integration into `/opt/zavod`, create mode-600 backups before updating
state, run safe production preflight, and request the exact single-use phrase:

```text
AUTODIAGNOSE GiRrLzdan5Gz31ngH4zgxk6ybYaryNVCSLdAJyn1pump WITH 0.03 SOL
```

Do not run the transaction-capable batch until that phrase is returned in a
new user message.
