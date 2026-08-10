# Workflow

1. Create a Track and finish a testable Brief.
2. Version Lyrics and Prompt records.
3. Perform provider work manually and register Generation/Edit events.
4. Import only selected assets; rejected files stay under the configured
   retention policy.
5. Record blind listening and selection as human Review records.
6. Register exports/renders and run `validate --level post`.
7. Prepare a Release Candidate. Freeze requires an explicit human action.
8. A human publishes the GitHub Release. Its assets become the authoritative
   public copies; the repository records their URLs and SHA-256 values.

Validation levels:

- `minimal`: records parse, IDs/refs/DAG/paths/state are valid.
- `post`: minimal plus files, hashes, LFS expectations, and technical metadata.
- `release`: post plus RC, listening gate, rights references, checksums, and
  policy snapshot references.

`PASS` means the required records exist and mechanical invariants hold. It is
not an artistic judgment or legal clearance.
