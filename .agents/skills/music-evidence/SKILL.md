---
name: music-evidence
description: Inspect an existing music-project-template evidence ledger and prepare, plan, or explicitly apply one verified generation, edit, export, render, human review, rights-evidence, or release-candidate event. Use when an actual music operation or accountable human judgment needs a sealed audit record, or when diagnosing ledger status and retrying a submission; do not use for project initialization, draft creative material, speculative provider activity, asset import, or publication.
---

# Music Evidence

Use the ledger as a separate system of record for facts that actually happened.
The source music project may be any kind of repository; it does not need to
adopt this template. Keep creative state, evidence state, and publication state
distinct.

## Establish the ledger

Obtain the intended ledger root from the user or from an unambiguous path in
the current task. Do not search broadly for ledgers and do not initialize one.
Verify both files exist:

```text
<ledger-root>/music.toml
<ledger-root>/scripts/music.py
```

Use absolute paths in commands and always pass the root explicitly before the
subcommand:

```bash
LEDGER_ROOT="/absolute/path/to/music-ledger"
python3 "$LEDGER_ROOT/scripts/music.py" --root "$LEDGER_ROOT" status
python3 "$LEDGER_ROOT/scripts/music.py" --root "$LEDGER_ROOT" validate --level minimal --json
```

If the ledger is not schema `1.0`, stop. Do not read, adapt, migrate, or
coordinate an older format. If validation fails, diagnose the reported facts;
do not apply an event to work around the failure.

## Decide whether a fact belongs

Only prepare one of these evidence events:

- `generation`: a provider generation that actually occurred
- `edit`: a provider edit that actually occurred
- `export`: a provider export or download that actually occurred
- `render`: a local toolchain render that actually occurred
- `review`: a real human listening decision
- `rights_evidence`: a real human rights assessment and public-safe reference
- `release_candidate`: a confirmed preparation of one exact reviewed master

Do not submit draft prompts, draft lyrics, unperformed plans, speculative
provider IDs, inferred listening, or guessed rights. Do not parse another
project's creative files and infer evidence from them. When required facts are
missing, prepare a draft envelope for review at most; do not apply it.

Asset import, project or Track creation, text hashing, retention, and
publication are separate operations outside this Skill. If an event needs an
asset reference that does not exist, report that prerequisite instead of
inventing a reference or importing bytes.

## Inspect the contract

Read the target ledger's `docs/event-api.md` and
`schemas/event-envelope.schema.json` when field details are needed. Do not copy
or maintain a second schema inside this Skill.

Every envelope must:

- contain exactly one event and no ledger-root field
- use schema `1.0`
- use a new lowercase canonical UUID as `submission_id`
- declare `submitted_by: {type, id}` where type is `human`, `agent`, or
  `automation`
- preserve the same submission ID and exact envelope for retries
- refer only to typed record and asset references already present in the target
  ledger

A `release_candidate` master is a file input, not a typed asset reference. A
user-supplied absolute master path may be outside the ledger, which supports a
separate evidence ledger for another music project. Do not import that file
merely to satisfy this Skill. The Release Plan Receipt must still bind its exact
path, SHA-256, size, and master-bound human listening gate.

Create a UUID without relying on uppercase `uuidgen` output:

```bash
python3 -c 'import uuid; print(uuid.uuid4())'
```

`submitted_by` identifies who entered the fact. It is not proof of identity.
For accountable human judgment, also require a user-supplied declaration:

- `review` uses `reviewed_by: {type: "human", id: "..."}`
- `rights_evidence` uses `assessed_by: {type: "human", id: "..."}`
- `release_candidate` uses `confirmed_by: {type: "human", id: "..."}`

Never derive these fields from the caller, set a human flag automatically, or
claim the CLI authenticated the person.

For rights evidence, use only a public-safe opaque `evidence_ref`. Never place
an absolute or `file://` path, credentials, query token, fragment, invoice,
contract, identity document, account screenshot, or consent file in the
ledger. A `confirmed` assessment requires the evidence reference and its exact
lowercase SHA-256.

## Plan

Write the proposed envelope to a temporary JSON file outside the ledger unless
the user requested a durable location. Planning is read-only with respect to
canonical event records:

```bash
ENVELOPE="/absolute/path/to/event.json"
python3 "$LEDGER_ROOT/scripts/music.py" --root "$LEDGER_ROOT" plan-event "$ENVELOPE"
```

Ordinary event plans are informational. They do not bind the preview record ID
and have no `plan_digest`; apply performs full validation again.

For `release_candidate`, save the complete JSON output as the Release Plan
Receipt. Present its resolved master identity, exact listening gates, rights
statuses, policy snapshots, expected release ID and paths, and gate outcome to
the user. Do not edit the receipt.

Rights outcomes are deliberately simple:

- any `known_unlicensed` record for the Track blocks release preparation
- referenced `confirmed` evidence yields `PASS`
- missing evidence or any other status requires `override_reason` plus a human
  `confirmed_by`, and yields `PASS_WITH_OVERRIDE`

Describe `PASS_WITH_OVERRIDE` only as an unresolved-rights override. Never call
it rights-cleared, approved for publication, or legally safe.

## Confirm and apply

Immediately before applying, show the user the ledger root, event type,
submission ID, submitter, domain-responsibility declaration when applicable,
important input/output references, and the expected outcome. Obtain explicit
confirmation for this ledger write. A request to draft, inspect, diagnose, or
plan is not confirmation to apply.

After confirmation, apply the exact envelope:

```bash
python3 "$LEDGER_ROOT/scripts/music.py" --root "$LEDGER_ROOT" apply-event \
  "$ENVELOPE" --confirm
```

For a release candidate, also pass the unchanged receipt:

```bash
PLAN_RECEIPT="/absolute/path/to/release-plan.json"
python3 "$LEDGER_ROOT/scripts/music.py" --root "$LEDGER_ROOT" apply-event \
  "$ENVELOPE" --plan-receipt "$PLAN_RECEIPT" --confirm
```

Treat the returned sealed event or release record as the authoritative receipt.
Report its status, record path, record ID, submission ID, and output binding.
Do not create a parallel receipt file.

On `ledger_busy`, wait until a later attempt and retry the exact envelope with
the same submission ID. Do not wait without a bound. On
`submission_conflict`, stop and surface the ID reuse. On `plan_stale`, create a
new release plan, show the changes, and obtain confirmation again. Never treat
a failed apply as proof that an event was recorded; inspect the structured
error or retry idempotently.

## Keep publication separate

A sealed Release Candidate records preparation, not publication. `PASS` is a
mechanical ledger outcome, not a legal conclusion. `PASS_WITH_OVERRIDE` retains
unresolved rights. Never invoke publication commands within this Skill. If the
user requests publication, report that it requires a separate governance
workflow and hand off without performing it here.

The ledger lock is intended for a local filesystem only. Do not claim support
for NFS, SMB, or cloud-synchronized directories.
