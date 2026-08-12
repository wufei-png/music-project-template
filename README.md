# Music Project Template

A Git-native template and standard-library Python toolkit for traceable,
AI-assisted music production. It records creative intent, provider operations,
file lineage, human decisions, rights-evidence references, and release freezes
without generating music or publishing on the user's behalf.

## Design boundary

- Plain TOML, Markdown, CSV, and JSON are the canonical source of truth.
- Generation/edit/export/render events are separate from file assets.
- Files use their complete SHA-256 as identity; events use stable `gNNN`,
  `eNNN`, `xNNN`, `rNNN`, and `rcNNN` identifiers.
- `minimal`, `post`, and `release` validation report mechanical completeness;
  they never decide artistic quality or legal rights.
- Provider profiles are declarative. The Suno profile records manual work and
  never logs in, scrapes, or calls unofficial endpoints.
- Provider selection inherits a Track override when present, otherwise the
  project default; `--provider` is an explicit per-command override.
- GitHub Release assets are the authoritative public copies of final masters.
  Final masters are not duplicated indefinitely in Git LFS.

## Quick start

After using this repository as a GitHub template, customize that checkout in place:

```bash
python3 scripts/music.py init . --project-id my-music --title "My Music"
```

Or initialize a separate existing directory:

```bash
python3 scripts/music.py init ../my-music --project-id my-music --title "My Music"
cd ../my-music
python3 scripts/music.py new-track --track-id night-rain --title "Night Rain"
python3 scripts/music.py validate --level minimal
```

The evidence API accepts one JSON Event Envelope at a time. The root is always
explicit for event operations:

```bash
python3 scripts/music.py --root "$PWD" plan-event /tmp/event.json
python3 scripts/music.py --root "$PWD" apply-event /tmp/event.json --confirm
```

`generation`, `edit`, `export`, `render`, `review`, `rights_evidence`, and
`release_candidate` share this interface. Each submission has a caller-created
UUID and may be retried with the same envelope. The resulting sealed record is
the authoritative receipt. Release candidates additionally require the exact
Release Plan Receipt returned by `plan-event`:

```bash
python3 scripts/music.py --root "$PWD" plan-event /tmp/release.json > /tmp/release-plan.json
python3 scripts/music.py --root "$PWD" apply-event /tmp/release.json \
  --plan-receipt /tmp/release-plan.json --confirm
```

The convenience commands such as `register-generation`, `record-review`, and
`freeze-release` route through the same event service; they do not bypass its
lock, validation, idempotency, or release-plan rules. Asset import, text
hashing, retention, validation, and publication remain separate commands.

Release preparation requires `ffprobe` (provided by FFmpeg) so a non-audio or
zero-duration file cannot become a release candidate. `PASS_WITH_OVERRIDE`
does not mean rights-cleared, and publication is always a separate decision.

This repository-local toolkit currently supports local filesystems only; it
does not promise correct locking on NFS, SMB, or cloud-synchronized folders.
See `docs/event-api.md`, `docs/workflow.md`, and `docs/domain-model.md` for the
contract.

## Optional Agent Skill

`.agents/skills/music-evidence` is a host-neutral procedure for inspecting an
already initialized ledger and preparing, planning, confirming, or retrying one
evidence event. A source music project can use a separate
music-project-template checkout as its evidence ledger by supplying that
ledger's root explicitly. The Skill does not parse creative workspaces,
initialize ledgers, import assets, publish releases, or duplicate the event
schema and transaction logic.

## Licenses

Repository code and template-owned content use the MIT License. Content added
by users has its own licensing boundary; see `CONTENT-LICENSE.md`.
