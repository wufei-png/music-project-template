# Evidence event API

## Envelope

`plan-event` and `apply-event` accept one JSON object matching
`schemas/event-envelope.schema.json`. The envelope never contains the ledger
root. Supply the root to the CLI before the subcommand:

```json
{
  "schema_version": "1.0",
  "event_type": "review",
  "submission_id": "438eaa41-34b5-476f-af3d-af886e6d8cb0",
  "submitted_by": {"type": "agent", "id": "music-assistant"},
  "reviewed_by": {"type": "human", "id": "listener-1"},
  "track_id": "night-rain",
  "payload": {
    "subject_ref": "asset:a-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "subject_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "decision": "selected",
    "blind_label": "B"
  }
}
```

`submitted_by.type` is `human`, `agent`, or `automation`. Reviews add
`reviewed_by`, rights assessments add `assessed_by`, and release candidates add
`confirmed_by`; those three responsibility fields require `type: human`. The
CLI validates declarations but does not authenticate identity. IDs are stable
audit labels, not accounts or signatures.

Use a new canonical UUID for a new fact. Retry the exact same envelope with the
same `submission_id`. If that ID already has a sealed record with the same
normalized envelope digest, `apply-event` returns it. Reusing the ID for
different content returns `submission_conflict`.

## Plan and apply

For ordinary events, planning is an optional informational preview. It does not
bind a record ID and returns no `plan_digest`; apply repeats full validation.

For a `release_candidate`, planning is mandatory. The Release Plan Receipt
binds the normalized envelope, resolved master identity, reviewed references,
rights and policy snapshots, expected release ID and paths, and relevant ledger
state. Apply validates the receipt digest and recomputes the plan while holding
the ledger lock. Drift returns `plan_stale`; plan and confirm again.

Both operations take the same per-ledger exclusive advisory lock. The default
timeout is five seconds and may be changed with `--lock-timeout`; waiting is
always bounded. A busy ledger returns structured JSON with code `ledger_busy`.
Retry later using the same `submission_id`.

Apply requires `--confirm`. Within one lock critical section it checks
idempotency, validates current state, allocates IDs, stages outputs, commits,
and writes the sealed authoritative record. The long-lived
`.music-ledger.lock` file is harmless; the operating system releases the lock
when a process exits. No PID or time-based stale-lock cleanup is used.

## Rights and release outcomes

Rights records contain a public-safe `evidence_ref` and optional SHA-256, not a
private filesystem locator, credentialed URL, query token, or secret. Keep
contracts, identity documents, invoices, account screenshots, and consent files
outside Git.

- Any `known_unlicensed` rights record for the Track blocks release-candidate creation.
- Referenced `confirmed` evidence produces `PASS`.
- Missing evidence or any other status requires only `override_reason` and a
  human `confirmed_by`, producing `PASS_WITH_OVERRIDE`.

Both the gate and sealed release record preserve the outcome, responsibility,
and override reason. `PASS_WITH_OVERRIDE` is never rights-cleared. Publication
remains a separate governance action.

## Schema boundary

Schema `1.0` is a reset before the first formal release. The toolkit neither
reads nor migrates older ledger formats. Recreate active ledgers from the
current template. This version promises advisory-lock behavior only on a local
filesystem, not NFS, SMB, or cloud-synchronized directories.
