from __future__ import annotations

import json
import os
import shutil
import tempfile
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
    if not track_path.is_file():
        raise FileNotFoundError(f"unknown track: {track_id}")
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


def _normalized_policy(root: Path, track_id: str) -> dict[str, Any]:
    policy = dict(effective_policy(root, track_id))
    if policy["mode"] == "external_archive":
        policy["resolved_archive_directory"] = str(
            _archive_directory(str(policy["archive_locator"]), root, track_id)
        )
    return policy


def _reserve_archive_target(
    archive_dir: Path,
    name: str,
    digest: str,
    reserved_targets: dict[Path, str],
) -> Path:
    target = (archive_dir / name).resolve()
    existing_digest = sha256_file(target) if target.exists() else reserved_targets.get(target)
    if existing_digest is not None and existing_digest != digest:
        target = (archive_dir / f"{digest}-{name}").resolve()
    if target.exists() and sha256_file(target) != digest:
        raise RuntimeError(f"archive target conflict: {target}")
    reserved_digest = reserved_targets.get(target)
    if reserved_digest is not None and reserved_digest != digest:
        raise RuntimeError(f"archive target conflict within plan: {target}")
    reserved_targets[target] = digest
    return target


def build_plan(root: Path, track_id: str | None = None) -> dict[str, Any]:
    base = root / "assets" / "candidates-local"
    track_ids = [track_id] if track_id else sorted(
        path.name for path in base.iterdir() if path.is_dir()
    ) if base.exists() else []
    now = datetime.now().astimezone()
    entries: list[dict[str, Any]] = []
    reserved_targets: dict[Path, str] = {}
    for current_track in track_ids:
        policy = _normalized_policy(root, current_track)
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
            archive_target = ""
            if eligible and action == "archive_then_delete":
                archive_target = str(
                    _reserve_archive_target(
                        Path(str(policy["resolved_archive_directory"])),
                        path.name,
                        sha256_file(path),
                        reserved_targets,
                    )
                )
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
                    "archive_target": archive_target,
                }
            )
    return {"created_at": now_iso(), "entries": entries}


def _archive_directory(locator: str, root: Path, track_id: str) -> Path:
    parsed = urlparse(locator)
    if parsed.scheme not in {"", "file"}:
        raise ValueError("v0.1 external archive supports only paths and file:// locators")
    raw_path = parsed.path if parsed.scheme == "file" else locator
    archive_path = Path(raw_path).expanduser()
    if not archive_path.is_absolute():
        archive_path = root / archive_path
    archive = archive_path.resolve()
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
        policy = _normalized_policy(root, track_id)
        if entry.get("policy") != policy:
            raise ValueError(f"retention policy changed after planning: {entry['path']}")
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
            archive_dir = Path(str(policy["resolved_archive_directory"]))
            target = _reserve_archive_target(
                archive_dir, path.name, digest, reserved_targets
            )
            if target == path:
                raise ValueError(f"archive target equals candidate source: {entry['path']}")
            if str(target) != str(entry.get("archive_target") or ""):
                raise ValueError(f"archive target changed after planning: {entry['path']}")
        preflighted.append(
            {
                "entry": entry,
                "track_id": track_id,
                "path": path,
                "digest": digest,
                "target": target,
            }
        )

    private_root = root / ".private"
    private_root.mkdir(parents=True, exist_ok=True)
    quarantine_root = Path(tempfile.mkdtemp(prefix="retention-", dir=private_root))
    prepared_outputs: list[dict[str, Any]] = []
    new_asset_records: set[Path] = set()
    preexisting_location_records = set(
        (root / "assets" / "records" / "locations").glob("al-*.toml")
    )
    new_location_records: set[Path] = set()
    new_archive_targets: list[Path] = []
    try:
        # Quarantine and verify the entire batch before creating evidence or deleting bytes.
        for index, prepared in enumerate(preflighted):
            entry = prepared["entry"]
            path = prepared["path"]
            digest = prepared["digest"]
            quarantine = quarantine_root / str(index) / path.name
            quarantine.parent.mkdir(parents=True)
            os.replace(path, quarantine)
            prepared["quarantine"] = quarantine
            if sha256_file(quarantine) != digest:
                raise RuntimeError(f"candidate changed after preflight: {entry['path']}")

        # Complete all archives before writing any canonical records.
        for prepared in preflighted:
            target = prepared["target"]
            locator = ""
            if target is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(prepared["quarantine"], target)
                    new_archive_targets.append(target)
                if sha256_file(target) != prepared["digest"]:
                    raise RuntimeError(f"archive verification failed: {target}")
                locator = target.as_uri()
            prepared["locator"] = locator

        # Only after the whole byte batch is safe do we create canonical evidence.
        for prepared in preflighted:
            entry = prepared["entry"]
            digest = prepared["digest"]
            asset_path = root / "assets" / "records" / f"a-{digest}.toml"
            asset_preexisted = asset_path.exists()
            asset_ref, record_path = create_asset_record(
                root,
                source=prepared["quarantine"],
                sha256=digest,
                size_bytes=prepared["quarantine"].stat().st_size,
                actor=actor,
                role="rejected_candidate",
                storage="external_archive" if prepared["locator"] else "metadata_only",
                track_ref=f"track:{prepared['track_id']}",
                local_path="",
                original_name=prepared["path"].name,
                archived_locator=prepared["locator"],
            )
            if record_path not in preexisting_location_records:
                new_location_records.add(record_path)
            if not asset_preexisted:
                new_asset_records.add(asset_path)
            prepared_outputs.append(
                {
                    "path": entry["path"],
                    "asset_ref": asset_ref,
                    "record": record_path.relative_to(root).as_posix(),
                    "archived_locator": prepared["locator"],
                }
            )
    except BaseException:
        for record_path in new_location_records:
            record_path.unlink(missing_ok=True)
        for asset_path in new_asset_records:
            asset_path.unlink(missing_ok=True)
        for target in reversed(new_archive_targets):
            target.unlink(missing_ok=True)
        for prepared in reversed(preflighted):
            quarantine = prepared.get("quarantine")
            original = prepared["path"]
            if isinstance(quarantine, Path) and quarantine.exists() and not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                os.replace(quarantine, original)
        shutil.rmtree(quarantine_root, ignore_errors=True)
        raise

    result = {"applied_at": now_iso(), "actor": actor, "applied": prepared_outputs}
    try:
        shutil.rmtree(quarantine_root)
    except OSError as exc:
        # The operation is committed and fully reported; retained quarantine is
        # safer than returning an exception after candidate paths were removed.
        result["cleanup_warning"] = str(exc)
        result["recovery_path"] = str(quarantine_root)
    return result


def plan_as_json(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True)
