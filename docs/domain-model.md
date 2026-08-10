# Domain model

Events and assets are independent:

- `generation`, `edit`, `export`, and `render` records describe what happened.
- `asset` records describe immutable bytes and use the complete SHA-256 as identity.
- `asset_location` records separately describe where those bytes were registered,
  their role, and the Track association. The same bytes can therefore be used in
  multiple Tracks without losing provenance.
- Event `input_refs` and `output_refs` connect the two.
- `review` and `rights_evidence` records capture human decisions and evidence
  references without claiming artistic or legal truth.
- A `release_candidate` points to a master asset, metadata, required human
  gates, rights evidence, and checksums.

Records use typed references such as `generation:g001`, `asset:a-<sha256>`, and
`asset_location:al-…`, and `review:lr001`. Sealed records include a canonical content hash. Corrections
create a new record with `supersedes`; they do not silently rewrite history.

Workflow state and validation state are separate. Only a human can record the
transitions to `selected`, `frozen`, and `released`.
