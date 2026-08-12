# Music Evidence Ledger

This context names the durable facts and release decisions recorded for a
Git-native music project. It deliberately separates creative material from
claims about provenance, review, rights, and publication readiness.

## Language

**Evidence Event**:
A single submitted fact about generation, editing, export, rendering, human
review, rights evidence, or a release candidate.
_Avoid_: Command, transaction, activity

**Event Envelope**:
The complete, normalized submission for one Evidence Event, including its
submission identity and declared submitter.
_Avoid_: Request body, batch

**Submission ID**:
A globally unique identity chosen before an Event Envelope is applied and
reused when retrying that same submission.
_Avoid_: Event ID, record ID

**Sealed Record**:
The authoritative, immutable result of an applied Event Envelope, including its
submission binding and output binding.
_Avoid_: Receipt, log entry

**Release Plan Receipt**:
An ephemeral preview binding one release-candidate envelope to its resolved
master, expected release identity and paths, and relevant ledger state.
_Avoid_: Release record, persistent receipt

**Creative Approval**:
A human judgment that music is creatively final. It does not imply rights
clearance or publication readiness.
_Avoid_: Release approval, clearance

**Rights Status**:
The declared result of a human rights-evidence assessment: `confirmed`,
`needs_review`, `missing`, or `known_unlicensed`.
_Avoid_: Legal truth, license guarantee

**Release Gate Status**:
The mechanical outcome attached to a Release Candidate: `PASS` or
`PASS_WITH_OVERRIDE`. Neither status is a publication decision.
_Avoid_: Rights-cleared, published

**Domain Responsibility**:
The declared human responsible for a review, rights assessment, or release
confirmation, distinct from the party that submitted the envelope.
_Avoid_: Submitter, authenticated identity
