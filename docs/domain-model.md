# Domain model

The repository separates four kinds of operation:

- Evidence events: generation, edit, export, render, review, rights evidence,
  and release candidate. These use one Event Envelope API.
- Asset operations: import and byte/location registration.
- Administrative operations: project and Track creation, text hashing, status,
  and validation.
- Governance operations: retention and publication.

Events and assets are independent:

- `generation`, `edit`, `export`, and `render` records describe what happened.
- `asset` records describe immutable bytes and use the complete SHA-256 as identity.
- `asset_location` records separately describe where those bytes were registered,
  their role, and the Track association. The same bytes can therefore be used in
  multiple Tracks without losing provenance. A temporary `release_staging`
  location may coexist with the later canonical `github_release` location.
- Event `input_refs` and `output_refs` connect the two.
- `review` and `rights_evidence` records capture declared human decisions and
  public-safe evidence references without claiming identity authentication,
  artistic truth, or legal truth.
- A `release_candidate` points to a master asset, metadata, required human
  gates, rights evidence, checksums, and a measurement-only audio analysis.
- Provider terms snapshots are provider-typed files whose exact SHA-256 is
  recorded on generation events and frozen into relevant releases.

Evidence Event Envelopes use caller-generated submission UUIDs and normalize to
an envelope digest. The resulting sealed record contains both values and its
complete output binding; that record is the authoritative application receipt.
Ordinary plans are informational. Release candidates alone require a
state-bound Release Plan Receipt before apply.

Records use typed references such as `generation:g001`, `export:x001`,
`asset:a-<sha256>`, `asset_location:al-…`, and `review:lr001`. Sealed records
include a canonical content hash. Corrections create a new record with
`supersedes`; they do not silently rewrite history.

Workflow state and validation state are separate. The declarations
`reviewed_by`, `assessed_by`, and `confirmed_by` assign human responsibility;
the local CLI does not authenticate them. Publication remains separate from
release-candidate preparation.
