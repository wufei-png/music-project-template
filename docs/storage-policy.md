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

Release-candidate apply builds a complete bundle below
`releases/.staging/<submission-id>/`, then atomically renames it into its final
`releases/rcNNN/` directory. An unrelated staging directory is never removed.
The sealed `rc.toml` binds every output and is the commit receipt.
`record-publication` later registers the matching GitHub download URL as a
canonical asset location; publication is not implied by release staging.
