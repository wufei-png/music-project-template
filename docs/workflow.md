# Workflow

1. Create a Track and finish a testable Brief.
2. Version Lyrics and Prompt records.
3. Perform provider work manually and register Generation/Edit events.
4. Import only selected assets; rejected files stay under the configured
   retention policy.
5. Record blind listening and selection as human Review records. A release
   listening gate includes the SHA-256 of the exact reviewed master.
6. Register exports/renders and run `validate --level post`.
7. Prepare a Release Candidate. Freeze requires an explicit human action,
   successful `post` validation, a positive-duration audio measurement from
   `ffprobe`, provider-typed policy snapshots, and complete human gates.
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

`PASS` means the required records exist and mechanical invariants hold. It is
not an artistic judgment or legal clearance.
