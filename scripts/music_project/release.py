from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from .assets import create_asset_record
from .io import atomic_write_toml, load_toml, now_iso, resolve_inside
from .records import common_record, seal_is_valid, seal_record
from .validation import validate


def _require_validation(root: Path, level: str) -> None:
    report = validate(root, level)
    if report["status"] == "PASS":
        return
    codes = ", ".join(sorted({issue["code"] for issue in report["errors"]}))
    raise ValueError(f"{level} validation failed before publication: {codes}")


def _github_publication_parts(url: str, expected_kind: str) -> tuple[str, str, str, str]:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("authoritative publication URLs must be canonical GitHub HTTPS URLs")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if expected_kind == "release" and len(parts) == 5 and parts[2:4] == ["releases", "tag"]:
        return parts[0], parts[1], parts[4], ""
    if expected_kind == "asset" and len(parts) == 6 and parts[2:4] == ["releases", "download"]:
        return parts[0], parts[1], parts[4], parts[5]
    raise ValueError(f"invalid GitHub {expected_kind} URL structure")


def record_publication(
    root: Path,
    *,
    release_id: str,
    actor: str,
    release_url: str,
    asset_url: str,
    asset_name: str,
    asset_sha256: str,
    confirmed: bool,
) -> Path:
    """Record a separate publication fact for an already frozen release."""
    if not confirmed:
        raise ValueError("record-publication requires --confirm")
    if not re.fullmatch(r"rc[0-9]{3,}", release_id):
        raise ValueError("release-id must use rcNNN form")
    release_dir = resolve_inside(root / "releases" / release_id, root)
    rc_path = resolve_inside(release_dir / "rc.toml", root)
    if not rc_path.is_file():
        raise FileNotFoundError(f"unknown release candidate: {release_id}")
    _require_validation(root, "release")
    rc = load_toml(rc_path)
    if rc.get("status") != "sealed" or not seal_is_valid(rc):
        raise ValueError("release candidate has an invalid seal")
    expected = str(rc.get("master_sha256") or "").removeprefix("sha256:")
    if asset_sha256.removeprefix("sha256:") != expected:
        raise ValueError("published asset SHA-256 does not match the frozen master")
    metadata = load_toml(
        resolve_inside(Path(str(rc.get("metadata_path") or "")), root, must_exist=True)
    )
    expected_tag = str(metadata.get("github_tag") or "")
    release_owner, release_repo, release_tag, _ = _github_publication_parts(
        release_url, "release"
    )
    asset_owner, asset_repo, asset_tag, url_asset_name = _github_publication_parts(
        asset_url, "asset"
    )
    if (release_owner, release_repo, release_tag) != (
        asset_owner,
        asset_repo,
        asset_tag,
    ):
        raise ValueError("release and asset URLs must identify the same repository and tag")
    if release_tag != expected_tag:
        raise ValueError("publication URL tag does not match frozen metadata")
    if url_asset_name != asset_name or Path(asset_name).name != asset_name:
        raise ValueError("publication asset name does not match the asset URL")

    publication_path = resolve_inside(release_dir / "publication.toml", root)
    if publication_path.exists():
        raise FileExistsError(f"publication record already exists: {publication_path}")
    frozen_master = resolve_inside(
        Path(str(rc.get("master_path") or "")), root, must_exist=True
    )
    locations_before = set(
        (root / "assets" / "records" / "locations").glob("al-*.toml")
    )
    _, canonical_location_path = create_asset_record(
        root,
        source=frozen_master,
        sha256=expected,
        size_bytes=int(rc.get("master_size_bytes") or frozen_master.stat().st_size),
        actor=actor,
        role="public_release_master",
        storage="github_release",
        track_ref=str(rc.get("track_ref") or ""),
        original_name=asset_name,
        archived_locator=asset_url,
    )
    record = common_record("publication", release_id, actor=actor)
    record.update(
        {
            "track_ref": str(rc.get("track_ref") or ""),
            "release_candidate_ref": f"release_candidate:{release_id}",
            "canonical_location_ref": f"asset_location:{canonical_location_path.stem}",
            "release_url": release_url,
            "asset_url": asset_url,
            "asset_name": asset_name,
            "asset_sha256": expected,
            "canonical_storage": "github_release",
            "published_at": now_iso(),
        }
    )
    try:
        atomic_write_toml(publication_path, seal_record(record))
    except BaseException:
        if canonical_location_path not in locations_before:
            canonical_location_path.unlink(missing_ok=True)
        raise
    return publication_path
