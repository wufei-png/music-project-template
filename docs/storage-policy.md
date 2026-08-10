# Storage policy

| Location | Canonical responsibility |
|---|---|
| Ordinary Git | Briefs, prompts, lyrics, records, reviews, metadata, licenses, hashes |
| Git LFS | Imported inputs, selected source audio, necessary stems, selected project milestones |
| GitHub Releases | Final masters, previews, artwork, public stem/DAW bundles |
| Local/cold storage | Rejected generations, temporary renders, caches, rebuildable analysis |

Rejected-candidate policy is stored in `music.toml`, not compiled into Python.
Supported modes are `metadata_only`, `timed_local`, and `external_archive`.
The template default is `timed_local` with 90 days. Track policy may override
the Portfolio policy. Applying deletion or movement requires an explicit human
confirmation and always starts from a reviewable plan. Relative archive
locators are resolved from the project root, and apply rejects policy or file
drift after planning.
The apply phase quarantines and verifies the complete eligible batch before it
creates canonical evidence; preparation failure restores every candidate.

Release staging is temporary. `record-publication` registers the matching
GitHub download URL as a canonical asset location; after that record is sealed,
the staged local copy may be removed.
