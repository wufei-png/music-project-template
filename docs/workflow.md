# Workflow

1. Create a Track and finish a testable Brief.
2. Version Lyrics and Prompt records.
3. Perform provider work manually and submit Generation/Edit Event Envelopes.
4. Import only selected assets; rejected files stay under the configured
   retention policy.
5. Record blind listening and selection as human Review events. A release
   listening gate includes the SHA-256 of the exact reviewed master.
6. Register exports/renders and run `validate --level post`.
7. Plan a Release Candidate, inspect its resolved master and gates, then apply
   the unchanged envelope with that Release Plan Receipt and explicit human
   confirmation. Preparation requires successful `post` validation, a
   positive-duration audio measurement from `ffprobe`, provider-typed policy
   snapshots, and a master-bound human listening gate. Missing or unconfirmed
   rights evidence requires a concise override reason; known-unlicensed
   material always blocks.
8. A human publishes the GitHub Release. Its assets become the authoritative
   public copies; the repository records their URLs, SHA-256 values, and a
   canonical `github_release` asset location. The matching temporary staging
   file may then be removed without invalidating later workflow checks.

Validation levels:

- `minimal`: records parse, IDs/refs/DAG/paths/state are valid.
- `post`: minimal plus files, hashes, LFS expectations, and technical metadata.
- `release`: post plus a sealed RC, a master-bound listening gate, rights
  references, checksums, a hash-bound audio analysis, and policy snapshots for
  every provider represented in the Track lineage, including the exact path and
  SHA-256 recorded by each generation event.

`PASS` means the required records exist and mechanical invariants hold.
`PASS_WITH_OVERRIDE` means the release candidate was mechanically created while
rights evidence remained missing or unconfirmed. Neither is an artistic
judgment, legal clearance, or publication decision.
