# Finalizer Resilience Design

## Goal

Preserve a secret-safe run manifest when an optional generated artifact has
unsafe or invalid content, without weakening filesystem-integrity checks or
workspace restoration.

## Established Evidence

- Three real single-mint runs preserved `guard-result.txt` and a sanitized
  `generated-hot_tokens.json`, but no generated routing artifact or manifest.
- A transaction-free fixture reproduced that footprint when the second optional
  artifact, `routing.json`, contained rejected content.
- Commit `c66d8d8` introduced mandatory optional-artifact JSON sanitization and
  the new rejection path.
- All existing finalizer tests pass, but none covers a valid first optional
  artifact followed by rejected second-artifact content.

## Non-Goals

- Do not modify the closed binary, production configuration, sender settings,
  markets, LUTs, wallet material, or authenticated RPC sources.
- Do not make path, ownership, mode, symlink, ancestor-identity, or destination
  integrity failures recoverable.
- Do not persist rejected bytes, exception strings, URLs, signatures, UUIDs,
  keys, wallet values, or private RPC values.
- Do not change live authorization, timeout, loss limits, cleanup, or retry
  behavior.

## Design

`scripts/mint_runner.py` continues treating filesystem identity, ownership,
mode, symlink, ancestor, and destination-integrity failures as fatal. Those
conditions can redirect reads or writes and must stop finalization.

Generated artifact *content* rejection is nonfatal because `hot_tokens.json`
and `routing.json` are optional evidence, not restoration or execution inputs.
The sanitizer uses a dedicated content-error type so callers can distinguish
rejected JSON/content from path-integrity failures without examining or
persisting exception text.

`_capture_generated_artifact` returns one fixed status:

- `captured` — the artifact existed, sanitized successfully, and was written;
- `missing` — the artifact did not exist;
- `rejected_content` — decoding, structure, depth, non-finite value,
  protected-key collision, or residual protected-content validation rejected
  it.

The manifest contains:

```json
{
  "artifact_status": {
    "hot_tokens.json": "captured",
    "routing.json": "rejected_content"
  }
}
```

Restoration remains in `finally`. Rejected optional content therefore cannot
prevent restoration or the safe manifest from being written.

## Error Handling

- Content rejection records only `rejected_content` and continues.
- Missing artifacts record only `missing`.
- Filesystem-integrity failures remain fatal and generic.
- Cleanup failure is never converted to success.
- A failure or interruption never triggers an automatic retry.
- State and CLI output remain free of rejected data and exception text.

## Testing

All production changes use red-green-refactor.

- valid hot-token content plus invalid routing content writes a manifest,
  records `routing.json = rejected_content`, restores the workspace, and exits
  successfully;
- missing optional artifacts record `missing`;
- valid optional artifacts record `captured`;
- unsafe source mode, symlink, ownership, ancestor swap, or destination identity
  remains fatal;
- rejected content never appears in manifest, state, stdout, or stderr;
- existing finalization, restoration, signal, and failure tests remain green.

## Acceptance Criteria

- A production-shaped rejected routing artifact no longer prevents a manifest.
- Manifest artifact statuses use only the three fixed values.
- Filesystem-integrity violations remain fail-closed.
- Production config and binary hashes remain unchanged.
- No transaction-capable command is needed to verify this change.
