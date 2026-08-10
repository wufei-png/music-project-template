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

Common commands:

```bash
python3 scripts/music.py status
python3 scripts/music.py register-generation --help
python3 scripts/music.py register-edit --help
python3 scripts/music.py register-export --help
python3 scripts/music.py record-review --help
python3 scripts/music.py safe-import --help
python3 scripts/music.py analyze-audio --help
python3 scripts/music.py retention-plan
python3 scripts/music.py freeze-release --help
```

Release freezing requires `ffprobe` (provided by FFmpeg) so a non-audio or
zero-duration file cannot be recorded as the final master.

The v0.1 delivery is repository-local and is not published as a PyPI package.
See `docs/workflow.md` and `docs/domain-model.md` for the contract.

## Licenses

Repository code and template-owned content use the MIT License. Content added
by users has its own licensing boundary; see `CONTENT-LICENSE.md`.
