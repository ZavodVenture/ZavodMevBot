# Selector Diagnostic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a guarded, sender-disabled selector diagnostic that can distinguish zero selected mints from missing discovery evidence without changing production trading filters.

**Architecture:** Extend the existing mint-run lifecycle with an explicit diagnostic mode. Preparation writes a private, fixed-name temporary config whose only baseline changes are `auto.force_two_mints = false`, `auto.filters.limit = 1`, and `bot.merge_mints = false`; the closed CLI receives that file through its documented `run --config ... --test-mode` arguments. The existing live lock, bounded supervision, restoration, one-use confirmation, loss thresholds, and no-retry behavior remain authoritative. Finalization stores only fixed-schema counts and statuses; raw logs and credential-bearing config bytes remain private.

**Tech Stack:** Bash, Python 3.12 standard library (`tomllib`, `json`, `re`, `unittest`, `unittest.mock`), the existing Zavod guard/runner, and Git.

## Global Constraints

- Never invoke the binary `run` command directly; every diagnostic launch goes through `scripts/run-guarded.sh`.
- No diagnostic launch may omit both `--test-mode` and the diagnostic validation profile.
- Do not change the production `config.toml`, sender settings, fees, tips, markets, LUT inputs, wallet material, RPC sources, loss limits, or timeout bounds.
- The temporary config must be a regular owner-controlled mode-600 file, must never be printed, and must be deleted on success, failure, signal, or restore.
- Do not print or persist config values, authenticated URLs, wallet values, API keys, UUIDs, signatures, or raw rejected artifact content.
- Preserve the existing workspace live lock, process-group cleanup, `0.025 SOL` early stop, `0.03 SOL` loss target, and single-attempt authorization.
- A sender-dispatch indicator in test mode is `test_mode_dispatch_violation`, terminates the child, and is never reported as success.
- Missing cleanup or restoration is never success. Never retry automatically.
- D0, D1, and D2 are separate later executions with separate exact approvals; this implementation plan authorizes no real run.
- Unit and fixture verification must not contact RPC, start the real binary, or send a transaction.

## File Map

- Modify `scripts/zavod_guard.py`: validate the diagnostic profile, construct the exact test-mode child argv, and fail closed on dispatch evidence.
- Modify `scripts/run-guarded.sh`: accept only a validated diagnostic mode/config pair and forward it to the Python guard.
- Modify `scripts/mint_runner.py`: create/validate/delete the temporary diagnostic config and produce the fixed selector summary.
- Modify `scripts/mint-run.sh`: add an explicit diagnostic entry path and exact diagnostic confirmation.
- Modify `tests/test_zavod_guard.py`, `tests/test_mint_runner.py`, and `tests/test_mint_run_shell.py`.
- Update `state/CURRENT.md` and `state/EXPERIMENTS.md` only after a material guarded diagnostic run, not for unit fixtures.

---

### Task 1: Make `--test-mode` an Enforced Guard Contract

**Files:**
- Modify: `scripts/zavod_guard.py:186-270,889-1010,1103-1140`
- Modify: `tests/test_zavod_guard.py`

**Interface:**
- Extend `run_guarded(config_path, timeout_seconds=300, profile="default", test_mode=False, workspace_root=None)`.
- The diagnostic profile is valid only with `test_mode=True`.
- Its child argv is exactly `[binary, "run", "--config", absolute_private_config, "--test-mode"]`.

- [ ] Add tests that assert the diagnostic profile rejects missing test mode, rejects non-mode-600 or outside-workspace config paths, passes the exact child argv, and leaves all existing live profiles unchanged.
- [ ] Add an output-pump fixture containing a sender-dispatch marker and assert the supervised result is the fixed `test_mode_dispatch_violation`, the process group is terminated, and no marker payload is copied into the result.
- [ ] Run those new tests and confirm RED:

```bash
python3 -m unittest \
  tests.test_zavod_guard.ConfigGuardTests.test_selector_diagnostic_requires_cardinality_controls \
  tests.test_zavod_guard.RunGuardedHardeningTests.test_selector_diagnostic_uses_exact_test_mode_argv \
  tests.test_zavod_guard.RunGuardedHardeningTests.test_test_mode_dispatch_violation_stops_child
```

Expected: failures because the profile, exact argv, and violation reason do not exist.

- [ ] Implement the smallest profile/argv/dispatch changes. Validate that `auto.enabled = true`, all static markets remain disabled, `auto.force_two_mints = false`, `auto.filters.limit = 1`, and `bot.merge_mints = false`. Keep existing sender validation and loss supervision intact.
- [ ] Run the same three tests and confirm `Ran 3 tests` and `OK`.
- [ ] Run the existing guard suite:

```bash
python3 -m unittest tests.test_zavod_guard
```

- [ ] Commit:

```bash
git add scripts/zavod_guard.py tests/test_zavod_guard.py
git commit -m "feat: enforce sender-disabled selector diagnostics"
```

---

### Task 2: Add a Private Diagnostic Config Lifecycle

**Files:**
- Modify: `scripts/mint_runner.py:497-780,793-870,1510-1610`
- Modify: `tests/test_mint_runner.py`

**Interface:**
- Add a diagnostic preparation flag and a fixed relative config name bound to the prepared run.
- Build bytes in memory, changing only the three approved cardinality assignments.
- Store only a SHA-256 integrity record and fixed diagnostic metadata; never store config values.
- `validate-live`, `restore`, `restore-active`, and finalization all verify and remove the private temporary config.

- [ ] Add fixture tests for: exact three-key transformation; duplicate/missing/wrong-type assignments rejected; mode 600; production config hash unchanged; config never present in runner stdout/state summaries; cleanup after prepare failure, restore, finalize, SIGINT-equivalent cleanup, and stale-active recovery.
- [ ] Add a test proving D1 can change only `ignore_offchain_bots` in addition to the baseline three keys, while D0 rejects any extra change. Add a D2 metadata test that permits exactly target plus one validated control mint without changing other settings.
- [ ] Run the new lifecycle tests and confirm RED:

```bash
python3 -m unittest \
  tests.test_mint_runner.MintRunnerTestCase.test_diagnostic_config_changes_only_cardinality_controls \
  tests.test_mint_runner.MintRunnerTestCase.test_diagnostic_config_rejects_ambiguous_assignments \
  tests.test_mint_runner.MintRunnerTestCase.test_diagnostic_config_is_private_and_production_unchanged \
  tests.test_mint_runner.FinalizationTests.test_diagnostic_config_removed_on_every_terminal_path
```

Expected: failures/errors because diagnostic preparation and cleanup do not exist.

- [ ] Implement the byte-preserving transformer with parse-before/parse-after checks, a fixed run-bound filename, `O_CREAT|O_EXCL` and mode 600, integrity metadata, and fail-closed deletion. Exception messages must be generic.
- [ ] Run the four tests and confirm `Ran 4 tests` and `OK`.
- [ ] Run all runner tests:

```bash
python3 -m unittest tests.test_mint_runner
```

- [ ] Commit:

```bash
git add scripts/mint_runner.py tests/test_mint_runner.py
git commit -m "feat: isolate selector diagnostic configuration"
```

---

### Task 3: Route Diagnostics Only Through the Guarded Shell

**Files:**
- Modify: `scripts/run-guarded.sh:5-150`
- Modify: `scripts/mint-run.sh:5-330`
- Modify: `tests/test_zavod_guard.py`
- Modify: `tests/test_mint_run_shell.py`

**Interface:**
- `mint-run.sh <MINT> --diagnostic d0 --timeout N` prepares but does not launch until the operator types `DIAGNOSE <MINT> FOR <N>`.
- It calls `run-guarded.sh` with the inherited lock, diagnostic profile, fixed run-bound config, and test-mode flag.
- D1/D2 arguments remain unavailable until separately prepared by the coordinator; no stacked probes or retries.

- [ ] Add shell-fixture tests that reject a missing/extra test-mode flag, arbitrary config paths, direct diagnostic guard calls without the lock, wrong confirmations, repeated confirmations, and timeout values outside 30–300.
- [ ] Add a positive fixture asserting exactly one fake guard launch and exact propagation of diagnostic mode/config without exposing config contents.
- [ ] Run the new shell tests and confirm RED:

```bash
python3 -m unittest \
  tests.test_zavod_guard.RunGuardedWrapperTests.test_diagnostic_requires_test_mode_and_fixed_config \
  tests.test_mint_run_shell.MintRunShellTests.test_d0_uses_exact_guarded_test_mode_launch \
  tests.test_mint_run_shell.MintRunShellTests.test_diagnostic_confirmation_is_single_use
```

Expected: failures because the diagnostic shell route does not exist.

- [ ] Implement the minimal parsing and forwarding changes. The normal `RUN ...` sender-capable path must remain byte-for-byte equivalent in its constructed guard arguments.
- [ ] Run the three tests and confirm `Ran 3 tests` and `OK`.
- [ ] Run all shell/guard wrapper tests and syntax checks:

```bash
python3 -m unittest tests.test_mint_run_shell tests.test_zavod_guard
bash -n scripts/mint-run.sh scripts/run-guarded.sh
```

- [ ] Commit:

```bash
git add scripts/run-guarded.sh scripts/mint-run.sh \
  tests/test_zavod_guard.py tests/test_mint_run_shell.py
git commit -m "feat: add guarded selector diagnostic route"
```

---

### Task 4: Produce the Fixed Selector Diagnostic Summary

**Files:**
- Modify: `scripts/mint_runner.py:814-910,1290-1490`
- Modify: `tests/test_mint_runner.py`

**Interface:**
- Parse only `Fetched <integer> mint list.` counts from the private mode-600 log.
- Manifest fields: refresh count, zero count, string-keyed histogram, min/max, target presence, pool/LUT/runtime counts, candidate-construction status, and dispatch status.
- Counts come from sanitized fixed-shape artifacts; unknown structure produces a fixed `unavailable` status, not raw text.

- [ ] Add fixtures for `Fetched 0/1/2`, empty logs, malformed lines, target absent/present, artifact count separation, and dispatch violation. Assert exact schema and absence of source URLs/mints other than the authorized target.
- [ ] Run the summary tests and confirm RED:

```bash
python3 -m unittest \
  tests.test_mint_runner.MintRunnerTestCase.test_selector_histogram_tracks_zero_one_two \
  tests.test_mint_runner.MintRunnerTestCase.test_selector_summary_separates_discovery_from_selection \
  tests.test_mint_runner.FinalizationTests.test_manifest_records_test_mode_dispatch_violation
```

Expected: failures because the fixed selector summary does not exist.

- [ ] Implement bounded regex parsing and fixed-schema artifact counters. Do not infer construction, simulation, dispatch, landing, swaps, or PnL from another stage.
- [ ] Run the three tests and confirm `Ran 3 tests` and `OK`.
- [ ] Run the complete transaction-free suite:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/mint_runner.py scripts/zavod_guard.py
bash -n scripts/mint-run.sh scripts/run-guarded.sh scripts/preflight.sh
git diff --check
```

- [ ] Commit:

```bash
git add scripts/mint_runner.py tests/test_mint_runner.py
git commit -m "feat: summarize selector diagnostics safely"
```

---

### Task 5: Verify Scope and Prepare—but Do Not Execute—D0

- [ ] Re-run the focused suites:

```bash
python3 -m unittest \
  tests.test_mint_runner \
  tests.test_mint_run_shell \
  tests.test_zavod_guard -q
bash -n scripts/mint-run.sh scripts/run-guarded.sh scripts/preflight.sh
python3 -m py_compile scripts/mint_runner.py scripts/zavod_guard.py
./zavod-mev-bot-rust-version-cli --version
./zavod-mev-bot-rust-version-cli run --help
./scripts/preflight.sh
```

The last three commands are read-only validation. Do not expose credential-bearing output.

- [ ] Verify the production config and binary hashes match the pre-implementation mode-600 baseline, no diagnostic config remains, the live lock is free, and no bot process remains.
- [ ] Run `git diff --check` and verify the implementation commit range contains only the planned scripts/tests.
- [ ] Produce a secret-safe D0 approval manifest containing: target `6NwarBvDkXhByqVp2Qkq5i9XbtA2B3Bwe8SWGu9vpump`, duration, exact guarded/test-mode argv description, config/binary fingerprints, `0.025/0.03 SOL` stops, no-retry statement, and the exact single-use confirmation phrase.
- [ ] Stop and obtain fresh explicit approval immediately before D0. Do not execute D0 as part of implementation.

## Acceptance Checklist

- [ ] Every behavioral test was observed RED before its implementation and GREEN afterward.
- [ ] The real binary was never launched by tests.
- [ ] Diagnostic launch is impossible without the live lock, bounded timeout, fixed private config, exact confirmation, diagnostic profile, and `--test-mode`.
- [ ] Production config and binary bytes are unchanged.
- [ ] Temporary credential-bearing config is absent after every terminal path.
- [ ] Sender dispatch evidence causes `test_mode_dispatch_violation`.
- [ ] D0 summary distinguishes discovery, selection, construction, and dispatch without inference.
- [ ] D1 and D2 remain separate, one-variable, freshly approved experiments.
