# Standard Guard 1200-Second Timeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permit an explicitly selected 1200-second default-profile guarded run without changing the 300-second default or extending non-default profiles.

**Architecture:** Keep the existing launcher/supervisor defense in depth. Add a distinct Python maximum for the default profile and mirror the same profile-aware validation in the shell launcher; do not change the supervisor loop or financial stop constants.

**Tech Stack:** Bash, Python 3 standard library, `unittest`.

## Global Constraints

- The default timeout remains exactly `300` seconds.
- The default profile accepts integer timeouts from `30` through `1200`.
- `single-mint-auto`, `selector-diagnostic`, and `mint-run.sh` remain limited to `30` through `300`.
- Early stop remains `0.025 SOL`; loss target remains `0.03 SOL`.
- Never print or copy protected configuration, wallet material, authenticated URLs, UUIDs, or API keys.
- Do not execute a live or transaction-capable command during implementation or verification.

---

### Task 1: Guard Timeout Boundaries

**Files:**
- Modify: `tests/test_zavod_guard.py`
- Modify: `scripts/zavod_guard.py`
- Modify: `scripts/run-guarded.sh`

**Interfaces:**
- Consumes: existing `run_guarded(config_path, timeout_seconds, profile, ...)` and `run-guarded.sh --timeout SECONDS --profile PROFILE`.
- Produces: `MAX_DEFAULT_TIMEOUT_SECONDS = 1200` and profile-aware validation at both launcher boundaries.

- [ ] **Step 1: Write failing Python and shell behavior tests**

Extend `RunGuardedHardeningTests.run_with_mocks` with
`timeout_seconds=DEFAULT_TIMEOUT_SECONDS` and pass that argument to both
`run_guarded` calls. Add:

```python
def test_run_guarded_accepts_1200_seconds_for_default_profile(self):
    result = self.run_with_mocks(timeout_seconds=1200)
    self.assertEqual(result["reason"], "child_exit")
```

Change the default-profile rejection cases to `(29, 1201)`, expecting
`"timeout must be from 30 through 1200 seconds"`, and add:

```python
def test_run_guarded_keeps_non_default_profiles_at_300_seconds(self):
    for profile in ("single-mint-auto", "selector-diagnostic"):
        with self.subTest(profile=profile):
            with self.assertRaisesRegex(
                GuardError,
                "timeout must be from 30 through 300 seconds",
            ):
                zavod_guard.run_guarded(
                    "unused-config.toml",
                    1200,
                    profile=profile,
                )
```

In `RunGuardedWrapperTests`, add a test that invokes the real shell wrapper
with `--live-confirmed --timeout 1200`, expects exit `0`, and checks that the
fake Python boundary receives `"--timeout-seconds", "1200"`. Change the
default rejection cases to `("29", "1201", "invalid")`. Add a non-default
test asserting exit `64` for `single-mint-auto` and `selector-diagnostic`
with timeout `1200`.

- [ ] **Step 2: Run focused tests and confirm the expected RED failures**

Run:

```bash
python3 -m unittest \
  tests.test_zavod_guard.RunGuardedHardeningTests.test_run_guarded_accepts_1200_seconds_for_default_profile \
  tests.test_zavod_guard.RunGuardedHardeningTests.test_run_guarded_defensively_rejects_timeout_outside_bounds \
  tests.test_zavod_guard.RunGuardedHardeningTests.test_run_guarded_keeps_non_default_profiles_at_300_seconds \
  tests.test_zavod_guard.RunGuardedWrapperTests.test_accepts_maximum_default_timeout \
  tests.test_zavod_guard.RunGuardedWrapperTests.test_rejects_timeout_outside_bounds \
  tests.test_zavod_guard.RunGuardedWrapperTests.test_non_default_profiles_reject_extended_timeout
```

Expected: the 1200-second default-profile acceptance tests fail under the
existing 300-second ceiling; 1201 is incorrectly accepted by the shell test
fixture or produces the old validation behavior.

- [ ] **Step 3: Implement minimal profile-aware bounds**

In `scripts/zavod_guard.py`, keep:

```python
DEFAULT_TIMEOUT_SECONDS = 300
```

and add:

```python
MAX_DEFAULT_TIMEOUT_SECONDS = 1200
```

In `run_guarded`, select:

```python
max_timeout_seconds = (
    MAX_DEFAULT_TIMEOUT_SECONDS if profile == "default"
    else DEFAULT_TIMEOUT_SECONDS
)
```

Validate against that value and interpolate it into the `GuardError`.

In `scripts/run-guarded.sh`, update usage to show the default profile's
`30..1200` bound. After profile validation, select `max_timeout_seconds=1200`
for `default` and `300` otherwise, then validate the parsed integer against
that maximum. Keep the default assignment `timeout_seconds=300`.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the exact command from Step 2.

Expected: all six selected tests pass.

- [ ] **Step 5: Run the complete transaction-free test suite**

Run:

```bash
python3 -m unittest tests.test_zavod_guard tests.test_mint_runner tests.test_mint_run_shell
bash -n scripts/run-guarded.sh scripts/preflight.sh scripts/mint-run.sh
```

Expected: all tests pass and shell syntax validation exits `0`.

### Task 2: Operational Documentation and Safe Readiness Check

**Files:**
- Modify: `runbooks/first-live-run.md`
- Modify: `state/CURRENT.md`
- Modify: `state/EXPERIMENTS.md`

**Interfaces:**
- Consumes: the verified default-profile timeout contract from Task 1.
- Produces: operator-visible instructions distinguishing the 300-second default from the 1200-second explicit maximum.

- [ ] **Step 1: Update the runbook**

State that the default remains 300 seconds and the default profile may be
explicitly extended to 1200 seconds:

```bash
./scripts/run-guarded.sh --live-confirmed --timeout 1200
```

Retain the existing warning that transactions in flight prevent an absolute
guarantee of the `0.03 SOL` target.

- [ ] **Step 2: Run safe readiness checks**

Run the read-only preflight with output redacted in memory, then verify no
live process exists, the live lock is free, and configuration file modes are
`600`. Do not run `run` or invoke the guarded launcher with `--live-confirmed`.

- [ ] **Step 3: Record material checks**

Append concise dated entries to `state/CURRENT.md` and
`state/EXPERIMENTS.md` recording the new explicit maximum, unchanged safety
thresholds/default, test results, and preflight result. Do not record secrets,
addresses, endpoints, UUIDs, or API keys.

- [ ] **Step 4: Perform final verification**

Run:

```bash
python3 -m unittest tests.test_zavod_guard tests.test_mint_runner tests.test_mint_run_shell
bash -n scripts/run-guarded.sh scripts/preflight.sh scripts/mint-run.sh
git diff --check
```

Expected: the complete suite passes, syntax checks exit `0`, and the diff has
no whitespace errors.

- [ ] **Step 5: Review scope**

Confirm from the diff that no protected configuration, loss constant, early
stop constant, sender setting, diagnostic maximum, mint-run maximum, or live
execution path changed outside the approved timeout validation and
documentation scope.
