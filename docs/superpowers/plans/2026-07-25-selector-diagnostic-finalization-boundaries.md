# Selector Diagnostic Finalization Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent D0 finalization from persisting full generated artifacts and prevent byte-cap-truncated log tails from becoming selector evidence.

**Architecture:** Diagnostic finalization sanitizes and parses optional artifacts in memory, while normal finalization retains its existing persisted capture path. The streaming log reader distinguishes physical EOF from a configured byte cap and yields a buffered unterminated tail only for physical EOF.

**Tech Stack:** Python 3 standard library, `unittest`, existing Zavod runner helpers.

## Global Constraints

- Do not access or print `config.toml`, `wallet.enc`, authenticated RPC URLs, UUIDs, or API keys.
- Do not run the production binary, `run`, `run-guarded.sh`, or any transaction-capable command.
- Preserve normal non-diagnostic artifact capture behavior.
- Diagnostic results retain only fixed-schema counts/statuses, never full generated artifact content.
- Follow RED → verify RED → minimal GREEN → verify GREEN for each task.
- Modify only `scripts/mint_runner.py` and `tests/test_mint_runner.py`.

---

### Task 1: Keep diagnostic artifact evidence in memory

**Files:**
- Modify: `scripts/mint_runner.py`
- Test: `tests/test_mint_runner.py`

**Interfaces:**
- Consumes: `_read_owned_file_at`, `_sanitize_generated_artifact`, `_selector_diagnostic_summary`, `OPTIONAL_FILES`.
- Produces: an in-memory `{name: parsed_value}` mapping and existing `artifact_status` mapping for diagnostics; no `generated-*.json` files.

- [ ] **Step 1: Write the failing diagnostic finalization test**

Extend `test_diagnostic_finalize_uses_sanitized_selector_evidence_only` so its sanitized fixture includes the target and an unrelated entry, then assert:

```python
self.assertFalse(
    (prepared.result_dir / "generated-hot_tokens.json").exists()
)
self.assertFalse(
    (prepared.result_dir / "generated-routing.json").exists()
)
self.assertEqual(
    result["artifact_status"]["hot_tokens.json"],
    "captured",
)
self.assertNotIn("arb_mint_info", json.dumps(result, sort_keys=True))
```

Keep the exact target counts `3`, `153`, and `180`. Add or retain a normal-run assertion proving `generated-hot_tokens.json` and `generated-routing.json` are still captured outside diagnostics.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest \
  tests.test_mint_runner.FinalizationTests.test_diagnostic_finalize_uses_sanitized_selector_evidence_only -v
```

Expected: FAIL because `generated-hot_tokens.json` and/or `generated-routing.json` exists.

- [ ] **Step 3: Add an in-memory diagnostic artifact reader**

Add a helper with this behavior:

```python
def _read_generated_artifact_value(directories, name, policy):
    data = _read_owned_file_at(
        directories.root_fd,
        name,
        mode=0o600,
        missing_ok=True,
    )
    if data is None:
        return "missing", None
    try:
        sanitized = _sanitize_generated_artifact(data, policy)
        return "captured", json.loads(sanitized)
    except (GeneratedArtifactContentError, TypeError, ValueError):
        return "rejected_content", None
```

In `finalize_run`, keep `_capture_generated_artifact` unchanged for normal runs. For diagnostics, call the new helper for each optional file, retain only the status plus the in-memory parsed mapping, and pass that mapping directly to `_selector_diagnostic_summary`. Do not call `_captured_artifact_values` on the diagnostic path.

- [ ] **Step 4: Verify focused and runner GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest \
  tests.test_mint_runner.FinalizationTests.test_diagnostic_finalize_uses_sanitized_selector_evidence_only -v
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest tests.test_mint_runner -q
```

Expected: the focused test and complete runner suite pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/mint_runner.py tests/test_mint_runner.py
git commit -m "fix: keep diagnostic artifacts ephemeral"
```

### Task 2: Discard byte-cap-truncated log tails

**Files:**
- Modify: `scripts/mint_runner.py`
- Test: `tests/test_mint_runner.py`

**Interfaces:**
- Consumes: `_iter_owned_text_lines`, `SELECTOR_LOG_MAX_BYTES`, selector marker constants.
- Produces: the same line iterator contract except buffered unterminated text is emitted only after physical EOF, never after the byte cap.

- [ ] **Step 1: Write the failing cap-boundary tests**

Add a table-driven test whose cases are:

```python
cases = (
    ("Fetched 7 mint list.", "selected_count_histogram", {}),
    (
        mint_runner.SELECTOR_CONSTRUCTION_MARKER,
        "candidate_construction",
        "not_observed_in_log",
    ),
    (
        mint_runner.SELECTOR_DISPATCH_VIOLATION_MARKER,
        "dispatch",
        "not_applicable",
    ),
)
```

For each marker, write `(marker + "CONTINUES\n").encode()`, patch `SELECTOR_LOG_MAX_BYTES` to `len(marker.encode())`, call `_selector_diagnostic_summary`, and assert the expected empty/not-observed result. Add a reader regression with a file containing only `b"complete-without-newline"` and no cap truncation; `_iter_owned_text_lines` must still yield that line after physical EOF.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest \
  tests.test_mint_runner.MintRunnerTestCase.test_selector_log_discards_cap_truncated_marker_tail -v
```

Expected: FAIL because the current reader yields the cap-truncated buffer.

- [ ] **Step 3: Distinguish physical EOF from the byte cap**

Refactor the read loop around these explicit states:

```python
physical_eof = False
hit_byte_cap = False

if max_bytes is not None and bytes_read >= max_bytes:
    chunk = b""
    hit_byte_cap = True
else:
    chunk = os.read(descriptor, read_size)
    bytes_read += len(chunk)
    physical_eof = not chunk
    hit_byte_cap = (
        not physical_eof
        and max_bytes is not None
        and bytes_read >= max_bytes
    )

text = decoder.decode(
    chunk,
    final=physical_eof or hit_byte_cap,
)
```

After processing complete newline-terminated segments, yield `buffer` only when `physical_eof` is true. Break when either `physical_eof` or `hit_byte_cap` is true.

- [ ] **Step 4: Verify focused, runner, and full GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest \
  tests.test_mint_runner.MintRunnerTestCase.test_selector_log_discards_cap_truncated_marker_tail -v
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest tests.test_mint_runner -q
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -q
python3 -m py_compile scripts/mint_runner.py
bash -n scripts/mint-run.sh scripts/run-guarded.sh scripts/preflight.sh
git diff --check
```

Expected: all tests and static checks pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/mint_runner.py tests/test_mint_runner.py
git commit -m "fix: discard capped selector log tails"
```
