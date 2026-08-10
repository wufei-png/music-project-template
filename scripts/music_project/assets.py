from __future__ import annotations

import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .io import (
    atomic_write_toml,
    canonical_hash,
    load_config,
    load_toml,
    now_iso,
    relative_path,
    resolve_inside,
    sha256_file,
    validate_identifier,
)
from .records import common_record, seal_record


BUCKETS = {"inputs", "selected", "stems", "milestones"}
STORAGES = {
    "git_lfs",
    "release_staging",
    "github_release",
    "metadata_only",
    "external_archive",
}


def _asset_record_path(root: Path, asset_id: str) -> Path:
    return root / "assets" / "records" / f"{asset_id}.toml"


def _location_record_path(root: Path, location_id: str) -> Path:
    return root / "assets" / "records" / "locations" / f"{location_id}.toml"


def create_asset_record(
    root: Path,
    *,
    source: Path | None,
    sha256: str,
    size_bytes: int,
    actor: str,
    role: str,
    storage: str,
    track_ref: str,
    local_path: str = "",
    original_name: str = "",
    archived_locator: str = "",
) -> tuple[str, Path]:
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise ValueError("asset SHA-256 must be 64 lowercase hexadecimal characters")
    asset_id = f"a-{sha256}"
    path = _asset_record_path(root, asset_id)
    if path.exists():
        existing = load_toml(path)
        if existing.get("sha256") != sha256 or int(existing.get("size_bytes", -1)) != size_bytes:
            raise RuntimeError(f"asset identity collision: {asset_id}")
    else:
        record = common_record("asset", asset_id, actor=actor)
        record.update(
            {
                "sha256": sha256,
                "size_bytes": size_bytes,
                "registered_at": now_iso(),
            }
        )
        atomic_write_toml(path, seal_record(record))

    if storage not in STORAGES:
        raise ValueError(f"unknown asset storage: {storage}")
    if storage in {"git_lfs", "release_staging"} and not local_path:
        raise ValueError(f"{storage} requires local_path")
    if storage in {"external_archive", "github_release"} and not archived_locator:
        raise ValueError(f"{storage} requires archived_locator")
    asset_ref = f"asset:{asset_id}"
    location_identity = {
        "asset_ref": asset_ref,
        "track_ref": track_ref,
        "role": role,
        "storage": storage,
        "local_path": local_path,
        "original_name": original_name or (source.name if source else ""),
        "archived_locator": archived_locator,
    }
    location_id = f"al-{canonical_hash(location_identity)}"
    location_path = _location_record_path(root, location_id)
    if not location_path.exists():
        location = common_record("asset_location", location_id, actor=actor)
        location.update(location_identity)
        location["registered_at"] = now_iso()
        atomic_write_toml(location_path, seal_record(location))
    return asset_ref, location_path


def _safe_member(info: zipfile.ZipInfo) -> PurePosixPath:
    name = info.filename.replace("\\", "/")
    member = PurePosixPath(name)
    if not name or member.is_absolute() or ".." in member.parts:
        raise ValueError(f"unsafe ZIP member path: {info.filename}")
    unix_mode = (info.external_attr >> 16) & 0o170000
    if unix_mode == stat.S_IFLNK:
        raise ValueError(f"ZIP symlink is not allowed: {info.filename}")
    if unix_mode not in {0, stat.S_IFREG}:
        raise ValueError(f"ZIP special file is not allowed: {info.filename}")
    return member


def inspect_zip(path: Path, limits: dict[str, Any]) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(path) as archive:
        entries = [entry for entry in archive.infolist() if not entry.is_dir()]
        if len(entries) > int(limits["max_entries"]):
            raise ValueError("ZIP exceeds max_entries")
        total = 0
        for entry in entries:
            _safe_member(entry)
            if entry.file_size > int(limits["max_file_bytes"]):
                raise ValueError(f"ZIP member exceeds max_file_bytes: {entry.filename}")
            total += entry.file_size
            if total > int(limits["max_total_bytes"]):
                raise ValueError("ZIP exceeds max_total_bytes")
            if entry.file_size and entry.compress_size == 0:
                raise ValueError(f"invalid ZIP compression size: {entry.filename}")
            if entry.compress_size:
                ratio = entry.file_size / entry.compress_size
                if ratio > float(limits["max_compression_ratio"]):
                    raise ValueError(f"ZIP compression ratio is too high: {entry.filename}")
        return entries


def _unique_destination(directory: Path, name: str, digest: str) -> Path:
    safe_name = Path(name).name
    if not safe_name or safe_name in {".", ".."}:
        safe_name = f"asset-{digest[:12]}"
    target = directory / safe_name
    if not target.exists() or sha256_file(target) == digest:
        return target
    return directory / f"{digest[:12]}-{safe_name}"


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _import_one(
    root: Path,
    source: Path,
    *,
    bucket: str,
    track_id: str,
    actor: str,
    role: str,
) -> dict[str, str]:
    digest = sha256_file(source)
    destination_dir = resolve_inside(root / "assets" / bucket / track_id, root)
    destination = resolve_inside(
        _unique_destination(destination_dir, source.name, digest), root
    )
    if not destination.exists():
        _atomic_copy(source, destination)
    if sha256_file(destination) != digest:
        raise RuntimeError(f"copy verification failed: {destination}")
    storage = "git_lfs"
    asset_ref, record_path = create_asset_record(
        root,
        source=source,
        sha256=digest,
        size_bytes=destination.stat().st_size,
        actor=actor,
        role=role,
        storage=storage,
        track_ref=f"track:{track_id}",
        local_path=relative_path(destination, root),
    )
    return {
        "asset_ref": asset_ref,
        "asset_record": f"assets/records/{asset_ref.removeprefix('asset:')}.toml",
        "asset_location_record": relative_path(record_path, root),
        "path": relative_path(destination, root),
        "sha256": digest,
    }


def safe_import(
    root: Path,
    source: Path,
    *,
    bucket: str,
    track_id: str,
    actor: str,
    role: str,
) -> list[dict[str, str]]:
    if bucket not in BUCKETS:
        raise ValueError(f"bucket must be one of: {', '.join(sorted(BUCKETS))}")
    validate_identifier(track_id, "track-id")
    if not (root / "tracks" / track_id / "track.toml").is_file():
        raise FileNotFoundError(f"unknown track: {track_id}")
    source = source.expanduser().resolve(strict=True)
    config = load_config(root)
    limits = config.get("import_limits") or {}
    required_limits = {
        "max_entries",
        "max_file_bytes",
        "max_total_bytes",
        "max_compression_ratio",
    }
    if not required_limits.issubset(limits):
        raise ValueError("music.toml is missing import_limits")
    if not zipfile.is_zipfile(source):
        return [
            _import_one(
                root,
                source,
                bucket=bucket,
                track_id=track_id,
                actor=actor,
                role=role,
            )
        ]

    entries = inspect_zip(source, limits)
    imported: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="music-import-") as temporary_name:
        temporary = Path(temporary_name)
        with zipfile.ZipFile(source) as archive:
            for entry in entries:
                member = _safe_member(entry)
                extracted = temporary / Path(*member.parts)
                extracted.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source_handle, extracted.open("wb") as target_handle:
                    shutil.copyfileobj(source_handle, target_handle)
                imported.append(
                    _import_one(
                        root,
                        extracted,
                        bucket=bucket,
                        track_id=track_id,
                        actor=actor,
                        role=role,
                    )
                )
    return imported


def resolve_asset_local_path(root: Path, record: dict[str, Any]) -> Path | None:
    local_path = str(record.get("local_path") or "")
    if not local_path:
        return None
    return resolve_inside(Path(local_path), root)
