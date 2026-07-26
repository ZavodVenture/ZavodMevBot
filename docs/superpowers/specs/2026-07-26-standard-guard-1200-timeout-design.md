# Standard Guard 1200-Second Timeout Design

## Goal

Allow one explicitly requested default-profile Zavod run to last up to 1200
seconds while keeping the default duration at 300 seconds and preserving all
existing loss controls.

## Scope

- `./scripts/run-guarded.sh --live-confirmed --timeout 1200` is accepted for
  the `default` profile.
- Omitting `--timeout` still selects 300 seconds.
- The minimum remains 30 seconds.
- Values above 1200 seconds are rejected.
- `single-mint-auto`, `selector-diagnostic`, and `mint-run.sh` retain their
  existing maximum of 300 seconds.
- The early-stop threshold remains 0.025 SOL.
- The first-run loss target remains 0.03 SOL.
- The live command still requires `--live-confirmed`; this change does not
  authorize or execute a transaction-capable command.

## Design

Separate the Python supervisor's default duration from its maximum duration.
`DEFAULT_TIMEOUT_SECONDS` remains `300`, and a new
`MAX_DEFAULT_TIMEOUT_SECONDS = 1200` defines the upper bound for the default
profile. The defensive validator in `run_guarded` applies the 1200-second
maximum only when `profile == "default"` and applies the existing 300-second
maximum to every other profile.

The shell launcher mirrors the same profile-aware bounds after parsing all
arguments: the default profile accepts `30..1200`, while other profiles accept
`30..300`. Usage and validation messages state the applicable limits without
changing confirmation, locking, preflight, process-group shutdown, balance
monitoring, or loss-stop behavior.

## Tests

Regression tests cover both validation layers:

- the shell launcher accepts `--timeout 1200` for the default profile;
- the shell launcher rejects `1201`;
- the default remains `300`;
- non-default profiles reject `1200`;
- the Python supervisor accepts a default-profile timeout of `1200`;
- the Python supervisor rejects a default-profile timeout of `1201`;
- the Python supervisor rejects `1200` for non-default profiles.

The focused guard test suite and safe preflight must pass. No live command is
part of verification.

## Documentation and State

Update `runbooks/first-live-run.md` to distinguish the 300-second default from
the explicitly selected 1200-second maximum. After the material checks, add a
concise record to `state/CURRENT.md` and `state/EXPERIMENTS.md`. Do not include
configuration values, authenticated endpoints, wallet material, UUIDs, or API
keys.

## Success Criteria

The guarded command with an explicit 1200-second timeout reaches the normal
preflight and supervisor path for the default profile, while 1201 seconds and
extended non-default profiles fail before launch. Existing safety thresholds,
default duration, explicit approval requirement, and guarded shutdown behavior
remain unchanged.
