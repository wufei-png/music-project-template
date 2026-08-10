from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .assets import create_asset_record
from .io import (
    load_config,
    load_toml,
    now_iso,
    resolve_inside,
    sha256_file,
    validate_identifier,
)


VALID_MODES = {"metadata_only", "timed_local", "external_archive"}


def effective_policy(root: Path, track_id: str) -> dict[str, Any]:
    validate_identifier(track_id, "track-id")
    project = dict(load_config(root).get("candidate_retention") or {})
    track_path = root / "tracks" / track_id / "track.toml"
    if track_path.is_file():
        override = dict(load_toml(track_path).get("candidate_retention") or {})
        if override and not override.get("inherit", True):
            for key in ("mode", "days", "archive_locator"):
                if override.get(key) not in (None, ""):
                    project[key] = override[key]
    mode = project.get("mode")
    if mode not in VALID_MODES:
        raise ValueError(f"candidate retention mode must be one of: {', '.join(sorted(VALID_MODES))}")
    if mode == "timed_local" and int(project.get("days", -1)) < 0:
        raise ValueError("timed_local retention requires non-negative days")
    if mode == "external_archive" and not project.get("archive_locator"):
        raise ValueError("external_archive retention requires archive_locator")
    return project


def build_plan(root: Path, track_id: str | None = None) -> dict[str, Any]:
    base = root / "assets" / "candidates-local"
    track_ids = [track_id] if track_id else sorted(
        path.name for path in base.iterdir() if path.is_dir()
    ) if base.exists() else []
    now = datetime.now().astimezone()
    entries: list[dict[str, Any]] = []
    for current_track in track_ids:
        policy = effective_policy(root, current_track)
        directory = base / current_track
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            age_days = (now - modified).total_seconds() / 86400
            mode = policy["mode"]
            eligible = mode != "timed_local" or age_days >= int(policy["days"])
            action = {
                "metadata_only": "record_then_delete",
                "timed_local": "record_then_delete",
                "external_archive": "archive_then_delete",
            }[mode]
            entries.append(
                {
                    "track_id": current_track,
                    "path": path.relative_to(root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "age_days": round(age_days, 2),
                    "policy": policy,
                    "eligible": eligible,
                    "action": action if eligible else "keep",
                }
            )
    return {"created_at": now_iso(), "entries": entries}


def _archive_directory(locator: str, root: Path, track_id: str) -> Path:
    parsed = urlparse(locator)
    if parsed.scheme not in {"", "file"}:
        raise ValueError("v0.1 external archive supports only paths and file:// locators")
    raw_path = parsed.path if parsed.scheme == "file" else locator
    archive = Path(raw_path).expanduser().resolve()
    forbidden = {Path("/").resolve(), Path.home().resolve(), root.resolve()}
    if archive in forbidden:
        raise ValueError("archive_locator is too broad")
    candidate_base = (root / "assets" / "candidates-local").resolve()
    if (
        archive == candidate_base
        or archive in candidate_base.parents
        or candidate_base in archive.parents
    ):
        raise ValueError("archive_locator must not overlap candidate storage")
    return archive / track_id


def apply_plan(
    root: Path, plan: dict[str, Any], actor: str, *, confirmed: bool = False
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("retention application requires explicit confirmation")
    preflighted: list[dict[str, Any]] = []
    reserved_targets: dict[Path, str] = {}
    for entry in plan.get("entries", []):
        if not entry.get("eligible"):
            continue
        track_id = validate_identifier(str(entry.get("track_id") or ""), "track-id")
        path = resolve_inside(Path(str(entry.get("path") or "")), root, must_exist=True)
        candidate_root = (root / "assets" / "candidates-local" / track_id).resolve()
        try:
            path.relative_to(candidate_root)
        except ValueError as exc:
            raise ValueError(
                f"retention plan path is outside the candidate area: {entry.get('path')}"
            ) from exc
        if not path.is_file():
            raise ValueError(f"retention plan path is not a file: {entry['path']}")
        digest = sha256_file(path)
        if digest != entry["sha256"]:
            raise RuntimeError(f"candidate changed after planning: {entry['path']}")
        policy = effective_policy(root, track_id)
        modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        age_days = (datetime.now().astimezone() - modified).total_seconds() / 86400
        if policy["mode"] == "timed_local" and age_days < int(policy["days"]):
            raise ValueError(f"candidate is no longer eligible: {entry['path']}")
        expected_action = (
            "archive_then_delete"
            if policy["mode"] == "external_archive"
            else "record_then_delete"
        )
        if entry.get("action") != expected_action:
            raise ValueError(
                f"retention action does not match current policy: {entry['path']}"
            )
        target: Path | None = None
        if entry["action"] == "archive_then_delete":
            archive_dir = _archive_directory(
                str(policy["archive_locator"]), root, track_id
            )
            target = (archive_dir / path.name).resolve()
            if target == path:
                raise ValueError(f"archive target equals candidate source: {entry['path']}")
            existing_digest = (
                sha256_file(target) if target.exists() else reserved_targets.get(target)
            )
            if existing_digest is not None and existing_digest != digest:
                target = (archive_dir / f"{digest}-{path.name}").resolve()
            if target.exists() and sha256_file(target) != digest:
                raise RuntimeError(f"archive target conflict: {target}")
            reserved_digest = reserved_targets.get(target)
            if reserved_digest is not None and reserved_digest != digest:
                raise RuntimeError(f"archive target conflict within plan: {target}")
            reserved_targets[target] = digest
        preflighted.append(
            {
                "entry": entry,
                "track_id": track_id,
                "path": path,
                "digest": digest,
                "target": target,
            }
        )

    applied: list[dict[str, Any]] = []
    for prepared in preflighted:
        entry = prepared["entry"]
        track_id = prepared["track_id"]
        path = prepared["path"]
        digest = prepared["digest"]
        target = prepared["target"]
        locator = ""
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(path, target)
            if sha256_file(target) != digest:
                raise RuntimeError(f"archive verification failed: {target}")
            locator = target.as_uri()
        asset_ref, record_path = create_asset_record(
            root,
            source=path,
            sha256=digest,
            size_bytes=path.stat().st_size,
            actor=actor,
            role="rejected_candidate",
            storage="external_archive" if locator else "metadata_only",
            track_ref=f"track:{track_id}",
            local_path="",
            archived_locator=locator,
        )
        path.unlink()
        applied.append(
            {
                "path": entry["path"],
                "asset_ref": asset_ref,
                "record": record_path.relative_to(root).as_posix(),
                "archived_locator": locator,
            }
        )
    return {"applied_at": now_iso(), "actor": actor, "applied": applied}


def plan_as_json(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True)
