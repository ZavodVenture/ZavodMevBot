# Selector Diagnostic Finalization Boundaries

## Scope

This amendment closes two merge blockers in the existing D0 selector diagnostic. It does not change the operator command, guarded launch contract, loss limits, target selection, or normal live-run finalization.

## Diagnostic artifact boundary

For a diagnostic run, `hot_tokens.json` and `routing.json` may be read, sanitized, and parsed in memory solely to derive the fixed `selector_diagnostic` counts/statuses. Diagnostic finalization must not create or retain `generated-hot_tokens.json` or `generated-routing.json` in the result directory.

The manifest may retain the existing per-file `artifact_status` values (`captured`, `missing`, or `rejected_content`) and the existing fixed-schema `selector_diagnostic` fields. It must not contain the parsed artifact, pool identities, LUT identities, transaction arrays, unrelated mint entries, or other source content.

Normal non-diagnostic runs keep their current generated-artifact capture behavior unchanged.

## Bounded log boundary

The selector log reader may emit a final unterminated line only after observing physical EOF. Reaching `SELECTOR_LOG_MAX_BYTES` is not EOF. If the byte cap is reached with buffered text that has no newline, that incomplete tail must be discarded.

Complete newline-terminated lines read before the cap remain valid. This applies uniformly to refresh, construction, and dispatch markers so a cap-truncated prefix cannot create diagnostic evidence.

## Verification

- A diagnostic finalization fixture containing both target and unrelated sanitized entries produces the expected fixed counts/statuses and leaves no generated artifact files.
- A normal finalization regression still captures generated artifacts.
- Byte-cap tests split immediately after an otherwise exact refresh, construction, and dispatch marker while more bytes remain; none of those partial physical lines is accepted.
- Physical EOF still permits a final unterminated complete line, preserving the existing reader contract outside cap truncation.
- Full transaction-free discovery, Python compilation, shell syntax, and diff checks pass.

## Safety

No production binary, guarded run, RPC endpoint, wallet, protected configuration, or transaction-capable command is used for implementation or verification.
