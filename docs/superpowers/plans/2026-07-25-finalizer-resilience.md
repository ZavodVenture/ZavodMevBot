# Finalizer Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve a secret-safe run manifest and restore the workspace when optional generated artifact content is rejected, while retaining fail-closed filesystem-integrity behavior.

**Architecture:** Introduce a dedicated `GeneratedArtifactContentError` below `RunnerError`, and use it only for JSON decoding, structure, depth, non-finite value, protected-key collision, and residual protected-content failures. `_capture_generated_artifact` converts only that error to the fixed `rejected_content` status; filesystem reads and atomic writes continue raising `RunnerError`, while `finalize_run` records the two fixed-name artifact statuses in the manifest before its existing `finally` restoration.

**Tech Stack:** Python 3.12 standard library (`json`, `unittest`, `unittest.mock`), existing `scripts.mint_runner` and `scripts.zavod_guard` modules, Git.

## Global Constraints

- Do not modify the closed binary, production configuration, sender settings, markets, LUTs, wallet material, or authenticated RPC sources.
- Do not make path, ownership, mode, symlink, ancestor-identity, or destination integrity failures recoverable.
- Do not persist rejected bytes, exception strings, URLs, signatures, UUIDs, keys, wallet values, or private RPC values.
- Do not change live authorization, timeout, loss limits, cleanup, or retry behavior.
- Content rejection records only `rejected_content` and continues.
- Missing artifacts record only `missing`.
- Filesystem-integrity failures remain fatal and generic.
- Cleanup failure is never converted to success.
- A failure or interruption never triggers an automatic retry.
- State and CLI output remain free of rejected data and exception text.
- Keep restoration in the existing `finally` block.
- Manifest artifact statuses must use only `captured`, `missing`, and `rejected_content`.
- Production config and binary hashes must remain unchanged.
- No transaction-capable command is needed or permitted to verify this change.
- Never print or copy values from `config.toml`, `wallet.enc`, authenticated RPC URLs, UUIDs, or API keys.
- Do not run the bot or `run`; all verification uses transaction-free temporary-directory unit fixtures.

## File Map

- Modify `scripts/mint_runner.py`: define the content-only exception, return fixed artifact statuses, and include them in the manifest.
- Modify `tests/test_mint_runner.py`: add content taxonomy, status, production-shaped routing rejection, CLI secrecy, and path-error propagation coverage; update the obsolete expectation that invalid optional content aborts finalization.
- Do not modify any other file during implementation. In particular, do not update state files because no material production check or run occurs.

---

### Task 1: Establish the Generated-Artifact Content Error Boundary

**Files:**
- Modify: `scripts/mint_runner.py:73-79`
- Modify: `scripts/mint_runner.py:1292-1349`
- Test: `tests/test_mint_runner.py:747-820`

**Interfaces:**
- Consumes: `RunnerError`, `zavod_guard.ProtectedOutputPolicy`, `_sanitize_json_value(value, policy, depth=0)`, and `_sanitize_generated_artifact(data, policy)`.
- Produces: `GeneratedArtifactContentError(RunnerError)`, raised for artifact content rejection only. Filesystem/path functions continue raising `RunnerError`.

- [ ] **Step 1: Record protected-file baselines in the implementation shell**

Run:

```bash
export ZAVOD_FINALIZER_BASELINE_DIR="$(mktemp -d)"
sha256sum config.toml zavod-mev-bot-rust-version-cli \
  > "$ZAVOD_FINALIZER_BASELINE_DIR/protected.sha256"
```

Expected: exit 0 with no file contents printed. Keep this shell and environment variable for the final hash check.

- [ ] **Step 2: Add the failing content-error taxonomy test**

Add this method to `FinalizationTests` immediately after `write_guard_result`:

```python
    def test_generated_artifact_content_failures_use_dedicated_error(self):
        policy = mint_runner.zavod_guard.ProtectedOutputPolicy()
        nested = {}
        cursor = nested
        for _index in range(66):
            cursor["nested"] = {}
            cursor = cursor["nested"]
        collision = {
            "https://route-a.invalid/private": {"weight": 1},
            "https://route-b.invalid/private": {"weight": 2},
        }
        cases = {
            "invalid_utf8": (b"\xff", policy),
            "invalid_json": (b"not-json", policy),
            "scalar": (b'"scalar"', policy),
            "non_finite": (b'{"value":NaN}', policy),
            "too_deep": (json.dumps(nested).encode(), policy),
            "protected_key_collision": (
                json.dumps(collision).encode(),
                policy,
            ),
        }

        class ResidualPolicy:
            @staticmethod
            def redact_text(value):
                return value

            @staticmethod
            def contains_protected(value):
                return True

        cases["residual_protected_content"] = (
            b'{"safe":"value"}',
            ResidualPolicy(),
        )

        self.assertTrue(
            issubclass(
                mint_runner.GeneratedArtifactContentError,
                mint_runner.RunnerError,
            )
        )
        for label, (data, selected_policy) in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(
                    mint_runner.GeneratedArtifactContentError
                ):
                    mint_runner._sanitize_generated_artifact(
                        data,
                        selected_policy,
                    )
```

- [ ] **Step 3: Run the taxonomy test to verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_mint_runner.FinalizationTests.test_generated_artifact_content_failures_use_dedicated_error
```

Expected: ERROR because `scripts.mint_runner` has no attribute `GeneratedArtifactContentError`.

- [ ] **Step 4: Add the dedicated exception and route every content rejection through it**

Insert the exception directly after `RunnerError`:

```python
class RunnerError(RuntimeError):
    pass


class GeneratedArtifactContentError(RunnerError):
    pass
```

In `_sanitize_json_value` and `_sanitize_generated_artifact`, replace only these content-related raises:

```python
raise RunnerError("generated runtime artifact is invalid")
```

with:

```python
raise GeneratedArtifactContentError(
    "generated runtime artifact is invalid"
)
```

Make the same exception-class replacement in the decoding wrapper:

```python
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise GeneratedArtifactContentError(
            "generated runtime artifact is invalid"
        ) from exc
```

There must be seven resulting `GeneratedArtifactContentError` raise sites: maximum depth, non-finite float, sanitized-key collision, unsupported JSON value, decode/parse failure, invalid top-level shape, and residual protected content. The decode/parse site is one of those sites even though it catches multiple exception classes; verify the final count from source rather than changing unrelated `RunnerError` raises.

- [ ] **Step 5: Run the taxonomy test to verify GREEN**

Run:

```bash
python3 -m unittest \
  tests.test_mint_runner.FinalizationTests.test_generated_artifact_content_failures_use_dedicated_error
```

Expected: `Ran 1 test` and `OK`.

- [ ] **Step 6: Run the existing sanitization safety test**

Run:

```bash
python3 -m unittest \
  tests.test_mint_runner.FinalizationTests.test_finalize_sanitizes_every_persisted_artifact_and_result_file
```

Expected: `Ran 1 test` and `OK`; valid protected fields are redacted rather than rejected or leaked.

- [ ] **Step 7: Commit the content-error boundary**

Run:

```bash
git add scripts/mint_runner.py tests/test_mint_runner.py
git commit -m "refactor: distinguish generated artifact content errors"
```

Expected: one commit containing only the exception taxonomy and its focused test.

---

### Task 2: Return Fixed Artifact Statuses and Preserve the Manifest

**Files:**
- Modify: `scripts/mint_runner.py:1352-1365`
- Modify: `scripts/mint_runner.py:1443-1472`
- Test: `tests/test_mint_runner.py:1657-1720`
- Test: `tests/test_mint_runner.py:2053-2077`

**Interfaces:**
- Consumes: `GeneratedArtifactContentError`, `_read_owned_file_at`, `_sanitize_generated_artifact`, `_atomic_write_at`, `OPTIONAL_FILES`.
- Produces: `_capture_generated_artifact(directories, name, policy) -> str`, returning exactly `captured`, `missing`, or `rejected_content`; `finalize_run(...) -> dict` gains `artifact_status: dict[str, str]` with exactly the `hot_tokens.json` and `routing.json` keys.

- [ ] **Step 1: Extend the valid-artifact test with the `captured` contract**

In `test_finalize_copies_generated_artifacts_then_restores`, add this assertion immediately after the `finalize_run` call:

```python
        self.assertEqual(
            result["artifact_status"],
            {
                "hot_tokens.json": "captured",
                "routing.json": "captured",
            },
        )
```

- [ ] **Step 2: Add the failing missing-artifact status test**

Add this method after `test_finalize_copies_generated_artifacts_then_restores`:

```python
    def test_finalize_records_missing_optional_artifacts(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)

        result = mint_runner.finalize_run(
            self.root,
            prepared.run_id,
            guard_exit=0,
            started_at=100,
            ended_at=400,
            chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
        )

        expected = {
            "hot_tokens.json": "missing",
            "routing.json": "missing",
        }
        self.assertEqual(result["artifact_status"], expected)
        manifest = json.loads(
            (prepared.result_dir / "manifest.json").read_text()
        )
        self.assertEqual(manifest["artifact_status"], expected)
        self.assertEqual(
            set(manifest["artifact_status"].values()),
            {"missing"},
        )
        self.assertEqual(
            (self.root / "tokens.toml").read_bytes(),
            self.original_tokens,
        )
        self.assertFalse(
            (self.root / "state" / ".mint-run-active").exists()
        )
```

- [ ] **Step 3: Replace the obsolete invalid-content failure test with a successful rejection-status test**

Replace `test_finalize_rejects_non_json_generated_artifact_and_restores` in full with:

```python
    def test_finalize_records_non_json_artifact_as_rejected_content(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        generated = self.root / "hot_tokens.json"
        rejected = b"not-json-runtime-output"
        generated.write_bytes(rejected)
        generated.chmod(0o600)

        result = mint_runner.finalize_run(
            self.root,
            prepared.run_id,
            guard_exit=0,
            started_at=100,
            ended_at=400,
            chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
        )

        expected = {
            "hot_tokens.json": "rejected_content",
            "routing.json": "missing",
        }
        self.assertEqual(result["artifact_status"], expected)
        manifest_path = prepared.result_dir / "manifest.json"
        self.assertTrue(manifest_path.exists())
        self.assertEqual(
            json.loads(manifest_path.read_text())["artifact_status"],
            expected,
        )
        self.assertNotIn(
            rejected.decode(),
            manifest_path.read_text(),
        )
        self.assertFalse(
            (prepared.result_dir / "generated-hot_tokens.json").exists()
        )
        self.assertEqual(
            (self.root / "tokens.toml").read_bytes(),
            self.original_tokens,
        )
        self.assertFalse(
            (self.root / "state" / ".mint-run-active").exists()
        )
```

- [ ] **Step 4: Add the production-shaped second-artifact regression test**

Add this method immediately after the non-JSON status test:

```python
    def test_finalize_preserves_manifest_after_routing_key_collision(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        generated_hot = b'{"generated":true}\n'
        protected_routes = (
            "https://route-a.invalid/private",
            "https://route-b.invalid/private",
        )
        routing = {
            protected_routes[0]: {"weight": 1},
            protected_routes[1]: {"weight": 2},
        }
        (self.root / "hot_tokens.json").write_bytes(generated_hot)
        (self.root / "routing.json").write_text(json.dumps(routing))
        (self.root / "hot_tokens.json").chmod(0o600)
        (self.root / "routing.json").chmod(0o600)

        result = mint_runner.finalize_run(
            self.root,
            prepared.run_id,
            guard_exit=0,
            started_at=100,
            ended_at=400,
            chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
        )

        expected = {
            "hot_tokens.json": "captured",
            "routing.json": "rejected_content",
        }
        self.assertEqual(result["artifact_status"], expected)
        manifest_path = prepared.result_dir / "manifest.json"
        rendered = manifest_path.read_text()
        self.assertEqual(
            json.loads(rendered)["artifact_status"],
            expected,
        )
        self.assertTrue(
            (
                prepared.result_dir / "generated-hot_tokens.json"
            ).exists()
        )
        self.assertFalse(
            (prepared.result_dir / "generated-routing.json").exists()
        )
        for protected in protected_routes:
            self.assertNotIn(protected, rendered)
            for name in ("CURRENT.md", "EXPERIMENTS.md"):
                self.assertNotIn(
                    protected,
                    (self.root / "state" / name).read_text(),
                )
        self.assertEqual(
            (self.root / "tokens.toml").read_bytes(),
            self.original_tokens,
        )
        self.assertFalse(
            (self.root / "state" / ".mint-run-active").exists()
        )
```

- [ ] **Step 5: Add a direct path-error propagation test**

Add this method immediately before `test_finalize_rejects_artifact_symlink_and_restores`:

```python
    def test_capture_generated_artifact_does_not_downgrade_path_errors(self):
        directories = mint_runner._FinalizationDirectories(
            root_path=self.root,
            root_fd=-1,
            state_fd=-1,
            mint_runs_fd=-1,
            result_fd=-1,
        )
        policy = mint_runner.zavod_guard.ProtectedOutputPolicy()
        path_error = mint_runner.RunnerError(
            "private run paths are invalid"
        )

        with patch.object(
            mint_runner,
            "_read_owned_file_at",
            side_effect=path_error,
        ):
            with self.assertRaises(mint_runner.RunnerError) as raised:
                mint_runner._capture_generated_artifact(
                    directories,
                    "routing.json",
                    policy,
                )

        self.assertIs(raised.exception, path_error)
```

- [ ] **Step 6: Add a real CLI finalization secrecy test**

Add this method immediately after `test_finalize_cli_passes_exact_window_and_outputs_only_safe_paths`:

```python
    def test_finalize_cli_succeeds_without_leaking_rejected_routing(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        protected_routes = (
            "https://route-a.invalid/private",
            "https://route-b.invalid/private",
        )
        routing = {
            protected_routes[0]: {"weight": 1},
            protected_routes[1]: {"weight": 2},
        }
        (self.root / "hot_tokens.json").write_text(
            '{"generated":true}\n'
        )
        (self.root / "routing.json").write_text(json.dumps(routing))
        (self.root / "hot_tokens.json").chmod(0o600)
        (self.root / "routing.json").chmod(0o600)
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch.object(
                mint_runner,
                "aggregate_chain",
                return_value=self.zero_chain(),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = mint_runner.main(
                [
                    "--root",
                    str(self.root),
                    "finalize",
                    "--run-id",
                    prepared.run_id,
                    "--guard-exit",
                    "0",
                    "--started-at",
                    "100",
                    "--ended-at",
                    "400",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            stdout.getvalue(),
            "stop_reason=timeout\n"
            f"manifest=state/mint-runs/{prepared.run_id}/manifest.json\n",
        )
        persisted = stdout.getvalue() + stderr.getvalue()
        persisted += (
            prepared.result_dir / "manifest.json"
        ).read_text()
        for name in ("CURRENT.md", "EXPERIMENTS.md"):
            persisted += (self.root / "state" / name).read_text()
        for protected in protected_routes:
            self.assertNotIn(protected, persisted)
        self.assertEqual(
            json.loads(
                (prepared.result_dir / "manifest.json").read_text()
            )["artifact_status"]["routing.json"],
            "rejected_content",
        )
```

- [ ] **Step 7: Run the six contract and safety tests to verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_mint_runner.FinalizationTests.test_finalize_copies_generated_artifacts_then_restores \
  tests.test_mint_runner.FinalizationTests.test_finalize_records_missing_optional_artifacts \
  tests.test_mint_runner.FinalizationTests.test_finalize_records_non_json_artifact_as_rejected_content \
  tests.test_mint_runner.FinalizationTests.test_finalize_preserves_manifest_after_routing_key_collision \
  tests.test_mint_runner.FinalizationTests.test_capture_generated_artifact_does_not_downgrade_path_errors \
  tests.test_mint_runner.FinalizationTests.test_finalize_cli_succeeds_without_leaking_rejected_routing
```

Expected: the path-error propagation test passes as a fail-closed characterization, while the other tests fail/error because valid and missing results lack `artifact_status`, rejected content still raises `GeneratedArtifactContentError`, and the CLI cannot complete. At least one test must fail, proving RED before implementation.

- [ ] **Step 8: Return fixed statuses from `_capture_generated_artifact`**

Replace `_capture_generated_artifact` with:

```python
def _capture_generated_artifact(directories, name, policy):
    data = _read_owned_file_at(
        directories.root_fd,
        name,
        mode=0o600,
        missing_ok=True,
    )
    if data is None:
        return "missing"
    try:
        sanitized = _sanitize_generated_artifact(data, policy)
    except GeneratedArtifactContentError:
        return "rejected_content"
    _atomic_write_at(
        directories.result_fd,
        f"generated-{name}",
        sanitized,
    )
    return "captured"
```

The `try` block must wrap only `_sanitize_generated_artifact`. Do not wrap `_read_owned_file_at` or `_atomic_write_at`; their ownership, mode, symlink, source identity, and destination integrity errors must remain fatal.

- [ ] **Step 9: Collect statuses and add them to the manifest**

Replace the optional-artifact loop in `finalize_run` with:

```python
            artifact_status = {}
            for name in OPTIONAL_FILES:
                artifact_status[name] = _capture_generated_artifact(
                    directories,
                    name,
                    output_policy,
                )
```

Add the status map to `manifest` immediately after `aggregation_status`:

```python
                "aggregation_status": aggregation_status,
                "artifact_status": artifact_status,
                "log_events": log_summary,
```

Do not move the manifest write or the existing restoration `finally`.

- [ ] **Step 10: Run the six contract and safety tests to verify GREEN**

Run:

```bash
python3 -m unittest \
  tests.test_mint_runner.FinalizationTests.test_finalize_copies_generated_artifacts_then_restores \
  tests.test_mint_runner.FinalizationTests.test_finalize_records_missing_optional_artifacts \
  tests.test_mint_runner.FinalizationTests.test_finalize_records_non_json_artifact_as_rejected_content \
  tests.test_mint_runner.FinalizationTests.test_finalize_preserves_manifest_after_routing_key_collision \
  tests.test_mint_runner.FinalizationTests.test_capture_generated_artifact_does_not_downgrade_path_errors \
  tests.test_mint_runner.FinalizationTests.test_finalize_cli_succeeds_without_leaking_rejected_routing
```

Expected: `Ran 6 tests` and `OK`.

- [ ] **Step 11: Run focused restoration and manifest-integrity tests**

Run:

```bash
python3 -m unittest \
  tests.test_mint_runner.FinalizationTests.test_finalize_restores_exactly_once \
  tests.test_mint_runner.FinalizationTests.test_finalize_rejects_invalid_existing_result_file_mode_and_restores \
  tests.test_mint_runner.FinalizationTests.test_finalize_ancestor_swap_cannot_redirect_manifest_write
```

Expected: `Ran 3 tests` and `OK`; cleanup still occurs exactly once, and manifest path integrity remains fail-closed.

- [ ] **Step 12: Commit manifest resilience**

Run:

```bash
git add scripts/mint_runner.py tests/test_mint_runner.py
git commit -m "fix: preserve manifest for rejected optional content"
```

Expected: one commit containing the fixed status interface, manifest field, and end-to-end status tests.

---

### Task 3: Verify Fail-Closed Filesystem Safety and the Complete Suite

**Files:**
- Verify: `scripts/mint_runner.py:1352-1488`
- Verify: `tests/test_mint_runner.py:1986-2487`

**Interfaces:**
- Verifies: content rejection is nonfatal, while ownership, mode, symlink, source identity, destination integrity, and generic path failures remain fatal.

- [ ] **Step 1: Run the existing filesystem-integrity regression set**

Run:

```bash
python3 -m unittest \
  tests.test_mint_runner.FinalizationTests.test_finalize_rejects_artifact_symlink_and_restores \
  tests.test_mint_runner.FinalizationTests.test_finalize_rejects_dangling_artifact_symlink_and_restores \
  tests.test_mint_runner.FinalizationTests.test_finalize_rejects_generated_artifact_not_mode_600 \
  tests.test_mint_runner.FinalizationTests.test_finalize_rejects_symlink_destination_without_overwriting_target \
  tests.test_mint_runner.FinalizationTests.test_finalize_rejects_symlinked_state_ancestor_without_restoring \
  tests.test_mint_runner.FinalizationTests.test_finalize_rejects_symlinked_mint_runs_ancestor_without_restoring
```

Expected: `Ran 6 tests` and `OK`; source mode, source symlink, destination symlink, and ancestor identity violations remain fatal.

- [ ] **Step 2: Run all finalization tests**

Run:

```bash
python3 -m unittest tests.test_mint_runner.FinalizationTests
```

Expected: all finalization tests pass with `OK`; the exact count is the pre-change 71 plus the five newly added test methods.

- [ ] **Step 3: Run the complete transaction-free test suite**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: exit 0 and final `OK`; no sender, bot, guarded run, or live RPC command is invoked because the suite uses temporary fixtures and mocks.

- [ ] **Step 4: Verify formatting, scope, and protected hashes**

Run:

```bash
git diff --check
git diff --name-only HEAD~2..HEAD
sha256sum --check --status \
  "$ZAVOD_FINALIZER_BASELINE_DIR/protected.sha256"
```

Expected: all commands exit 0; the name list contains only `scripts/mint_runner.py` and `tests/test_mint_runner.py`; the hash check is silent, proving `config.toml` and `zavod-mev-bot-rust-version-cli` are byte-unchanged.

- [ ] **Step 5: Verify the final two-commit range**

Run:

```bash
git log --oneline -2
git diff --stat HEAD~2..HEAD
git diff --check HEAD~2..HEAD
```

Expected: the two focused commits are, newest first, `fix: preserve manifest for rejected optional content` and `refactor: distinguish generated artifact content errors`; the diff contains only `scripts/mint_runner.py` and `tests/test_mint_runner.py`, and the check exits 0.

## Final Acceptance Checklist

- [ ] A valid `hot_tokens.json` followed by production-shaped routing key collision writes a mode-600 manifest.
- [ ] The manifest records `hot_tokens.json = captured` and `routing.json = rejected_content`.
- [ ] Missing optional files record `missing`, and valid optional files record `captured`.
- [ ] The status map contains exactly the two optional filenames and only the three approved status values.
- [ ] Rejected bytes and protected strings appear in no generated artifact, manifest, state file, stdout, or stderr.
- [ ] Source mode, symlink, ancestor identity, and destination integrity violations remain fatal.
- [ ] Restoration remains in `finally`, happens exactly once, and no automatic retry is introduced.
- [ ] All finalization and full-suite tests pass without transaction-capable commands.
- [ ] `config.toml` and `zavod-mev-bot-rust-version-cli` match their pre-implementation hashes.
- [ ] Only `scripts/mint_runner.py` and `tests/test_mint_runner.py` changed.
