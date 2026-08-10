from __future__ import annotations

import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .analysis import analyze_audio
from .assets import create_asset_record
from .io import (
    atomic_write_text,
    atomic_write_toml,
    load_toml,
    now_iso,
    relative_path,
    resolve_inside,
    sha256_file,
    validate_identifier,
)
from .records import common_record, seal_is_valid, seal_record
from .validation import load_records, validate


def _require_validation(root: Path, level: str) -> None:
    report = validate(root, level)
    if report["status"] == "PASS":
        return
    codes = ", ".join(sorted({issue["code"] for issue in report["errors"]}))
    raise ValueError(f"{level} validation failed before publication: {codes}")


def _github_publication_parts(url: str, expected_kind: str) -> tuple[str, str, str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment:
        raise ValueError("authoritative publication URLs must be canonical GitHub HTTPS URLs")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if expected_kind == "release" and len(parts) == 5 and parts[2:4] == ["releases", "tag"]:
        return parts[0], parts[1], parts[4], ""
    if expected_kind == "asset" and len(parts) == 6 and parts[2:4] == ["releases", "download"]:
        return parts[0], parts[1], parts[4], parts[5]
    raise ValueError(f"invalid GitHub {expected_kind} URL structure")


def _record_scope(record: dict[str, Any]) -> str:
    if record.get("record_type") in {"portfolio", "asset", "asset_location"}:
        return "global"
    return str(record.get("track_ref") or record.get("record_id") or "global")


def _record_index(root: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    records, issues = load_records(root)
    if any(issue.severity == "error" for issue in issues):
        raise ValueError("cannot freeze while canonical TOML records fail to parse")
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for _, record in records:
        key = (_record_scope(record), str(record.get("record_id") or ""))
        index.setdefault(key, []).append(record)
    return index


def _unique_record(
    index: dict[tuple[str, str], list[dict[str, Any]]],
    reference: str,
    *,
    scope: str | None = None,
) -> dict[str, Any]:
    matches = (
        index.get((scope, reference), [])
        if scope is not None
        else [record for (_, record_id), records in index.items() if record_id == reference for record in records]
    )
    if len(matches) != 1:
        raise ValueError(f"reference is missing or ambiguous: {reference}")
    return matches[0]


def _next_rc(root: Path) -> str:
    maximum = 0
    for path in (root / "releases").glob("rc[0-9][0-9][0-9]"):
        try:
            maximum = max(maximum, int(path.name[2:]))
        except ValueError:
            pass
    return f"rc{maximum + 1:03d}"


def _copy_to_staging(source: Path, destination: Path) -> None:
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


def freeze_release(
    root: Path,
    *,
    track_id: str,
    master: Path,
    title: str,
    actor: str,
    listening_gate_refs: list[str],
    rights_refs: list[str],
    policy_snapshot_refs: list[str],
    release_id: str,
    override_reason: str,
    override_risks: list[str],
    confirmed: bool,
) -> tuple[Path, str]:
    if not confirmed:
        raise ValueError("freeze-release requires --confirm")
    validate_identifier(track_id, "track-id")
    if not (root / "tracks" / track_id / "track.toml").is_file():
        raise FileNotFoundError(f"unknown track: {track_id}")
    release_id = release_id or _next_rc(root)
    if not re.fullmatch(r"rc[0-9]{3}", release_id):
        raise ValueError("release-id must use rcNNN form")
    release_dir = resolve_inside(root / "releases" / release_id, root)
    if release_dir.exists():
        raise FileExistsError(f"release candidate already exists: {release_id}")
    master = master.expanduser().resolve(strict=True)
    if not master.is_file():
        raise ValueError("master must be a file")
    if not listening_gate_refs:
        raise ValueError("at least one listening gate is required")
    if not policy_snapshot_refs:
        raise ValueError("at least one policy snapshot reference is required")
    _require_validation(root, "post")
    digest = sha256_file(master)
    frozen_policy_snapshots: list[dict[str, str]] = []
    for reference in policy_snapshot_refs:
        try:
            snapshot = resolve_inside(Path(reference), root, must_exist=True)
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid policy snapshot reference: {reference}") from exc
        if not snapshot.is_file():
            raise ValueError(f"policy snapshot reference is not a file: {reference}")
        snapshot_relative = snapshot.relative_to(root.resolve()).parts
        if len(snapshot_relative) < 4 or snapshot_relative[:2] != ("policies", "platforms"):
            raise ValueError(f"policy snapshot is outside policies/platforms: {reference}")
        try:
            snapshot_record = load_toml(snapshot)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"invalid policy snapshot TOML: {reference}") from exc
        provider = str(snapshot_record.get("provider") or "")
        if snapshot_relative[2] != provider or not all(
            snapshot_record.get(field) for field in ("snapshot_id", "provider", "retrieved_at")
        ):
            raise ValueError(f"policy snapshot identity is incomplete or mismatched: {reference}")
        frozen_policy_snapshots.append(
            {
                "kind": "policy_snapshot",
                "path": relative_path(snapshot, root),
                "sha256": sha256_file(snapshot),
                "provider": provider,
            }
        )
    normalized_policy_refs = [item["path"] for item in frozen_policy_snapshots]

    index = _record_index(root)
    track_ref = f"track:{track_id}"
    track_records = [
        item
        for (scope, _), matches in index.items()
        if scope == track_ref
        for item in matches
    ]
    blockers = [
        str(item.get("record_id") or "")
        for item in track_records
        if item.get("record_type") == "rights_evidence"
        and item.get("human_status") == "known_unlicensed"
    ]
    if blockers:
        raise ValueError(
            "known_unlicensed is a non-overridable blocker: " + ", ".join(blockers)
        )
    event_providers = {
        str(item.get("provider") or "")
        for item in track_records
        if item.get("record_type") in {"generation", "edit", "export"}
        and item.get("provider")
    }
    frozen_providers = {item["provider"] for item in frozen_policy_snapshots}
    missing_providers = sorted(event_providers - frozen_providers)
    if missing_providers:
        raise ValueError(
            "missing policy snapshot for track provider: " + ", ".join(missing_providers)
        )
    frozen_policy_identities = {
        (item["path"], item["sha256"]) for item in frozen_policy_snapshots
    }
    required_generation_snapshots = {
        (
            str(item.get("terms_snapshot_ref") or ""),
            str(item.get("terms_snapshot_sha256") or ""),
        )
        for item in track_records
        if item.get("record_type") == "generation"
        and item.get("terms_snapshot_ref")
    }
    missing_generation_snapshots = sorted(
        required_generation_snapshots - frozen_policy_identities
    )
    if missing_generation_snapshots:
        raise ValueError(
            "release does not freeze the exact generation policy snapshot: "
            + ", ".join(path for path, _ in missing_generation_snapshots)
        )
    for reference in listening_gate_refs:
        review = _unique_record(index, reference, scope=track_ref)
        if review.get("status") != "sealed" or not seal_is_valid(review):
            raise ValueError(f"listening gate has an invalid seal: {reference}")
        if review.get("record_type") != "review" or review.get("decision") not in {"approved", "selected"} or not review.get("human_confirmed"):
            raise ValueError(f"invalid listening gate: {reference}")
        if str(review.get("subject_sha256") or "").removeprefix("sha256:") != digest:
            raise ValueError(f"listening gate is not bound to the frozen master: {reference}")

    unresolved: list[str] = []
    for reference in rights_refs:
        evidence = _unique_record(index, reference, scope=track_ref)
        if evidence.get("status") != "sealed" or not seal_is_valid(evidence):
            raise ValueError(f"rights evidence has an invalid seal: {reference}")
        if evidence.get("record_type") != "rights_evidence":
            raise ValueError(f"not a rights evidence record: {reference}")
        if not evidence.get("human_confirmed"):
            raise ValueError(f"rights evidence is not human-confirmed: {reference}")
        status = evidence.get("human_status")
        if status == "known_unlicensed":
            raise ValueError(f"known_unlicensed is a non-overridable blocker: {reference}")
        if status == "confirmed" and (
            not evidence.get("private_locator")
            or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("evidence_sha256") or ""))
        ):
            raise ValueError(f"confirmed rights evidence is incomplete: {reference}")
        if status != "confirmed":
            unresolved.append(reference)
    if not rights_refs:
        unresolved.append("no rights evidence references")
    if unresolved and not (override_reason and override_risks):
        raise ValueError(
            "rights evidence is incomplete; provide explicit override reason and unresolved risks"
        )

    staging_directory = root / "releases" / ".staging" / release_id
    if staging_directory.exists():
        raise FileExistsError(f"release staging area already exists: {release_id}")
    staging = resolve_inside(staging_directory / master.name, root)
    new_asset_path = root / "assets" / "records" / f"a-{digest}.toml"
    asset_preexisted = new_asset_path.exists()
    location_path: Path | None = None
    try:
        _copy_to_staging(master, staging)
        if sha256_file(staging) != digest:
            raise RuntimeError("staged master hash mismatch")
        release_dir.mkdir(parents=True)
        analysis_path = release_dir / "analysis.json"
        analyze_audio(root, staging, analysis_path)
        master_ref, location_path = create_asset_record(
            root,
            source=master,
            sha256=digest,
            size_bytes=staging.stat().st_size,
            actor=actor,
            role="release_master",
            storage="release_staging",
            track_ref=f"track:{track_id}",
            local_path=relative_path(staging, root),
        )

        metadata = {
            "schema_version": "0.1",
            "release_id": release_id,
            "title": title,
            "release_type": "single",
            "track_refs": [f"track:{track_id}"],
            "content_license": "project_defined",
            "publication_target": "github_release",
            "github_tag": f"release/{release_id}",
        }
        metadata_path = release_dir / "metadata.toml"
        atomic_write_toml(metadata_path, metadata)
        checksums_path = release_dir / "checksums.sha256"
        atomic_write_text(checksums_path, f"{digest}  {master.name}\n")

        gate = {
            "status": "PASS_WITH_OVERRIDE" if unresolved else "PASS",
            "checked_at": now_iso(),
            "confirmed_by": actor,
            "listening_gate_refs": listening_gate_refs,
            "rights_refs": rights_refs,
            "policy_snapshot_refs": normalized_policy_refs,
            "unresolved": unresolved,
            "note": "Evidence completeness is not a legal-rights determination.",
        }
        gate_path = release_dir / "gate.toml"
        atomic_write_toml(gate_path, gate)

        frozen_files = [
            {"kind": "metadata", "path": relative_path(metadata_path, root), "sha256": sha256_file(metadata_path)},
            {"kind": "gate", "path": relative_path(gate_path, root), "sha256": sha256_file(gate_path)},
            {"kind": "checksums", "path": relative_path(checksums_path, root), "sha256": sha256_file(checksums_path)},
            {"kind": "analysis", "path": relative_path(analysis_path, root), "sha256": sha256_file(analysis_path)},
            *frozen_policy_snapshots,
        ]

        record = common_record("release_candidate", release_id, actor=actor)
        record.update(
            {
                "track_ref": f"track:{track_id}",
                "master_ref": master_ref,
                "metadata_path": relative_path(metadata_path, root),
                "gate_path": relative_path(gate_path, root),
                "analysis_path": relative_path(analysis_path, root),
                "rights_refs": rights_refs,
                "listening_gate_refs": listening_gate_refs,
                "policy_snapshot_refs": normalized_policy_refs,
                "checksums_path": relative_path(checksums_path, root),
                "staging_path": relative_path(staging, root),
                "frozen_files": frozen_files,
                "publication_target": "github_release",
                "publication_status": "prepared",
                "frozen": {"value": True, "by": actor, "at": now_iso(), "git_commit": ""},
            }
        )
        if unresolved:
            record["human_override"] = {
                "reason": override_reason,
                "actor": actor,
                "timestamp": now_iso(),
                "unresolved_risks": override_risks,
            }
        rc_path = release_dir / "rc.toml"
        atomic_write_toml(rc_path, seal_record(record))
        _require_validation(root, "release")
        return rc_path, gate["status"]
    except BaseException:
        if location_path is not None:
            location_path.unlink(missing_ok=True)
        if not asset_preexisted:
            new_asset_path.unlink(missing_ok=True)
        shutil.rmtree(release_dir, ignore_errors=True)
        shutil.rmtree(staging.parent, ignore_errors=True)
        raise


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
    if not confirmed:
        raise ValueError("record-publication requires --confirm")
    if not re.fullmatch(r"rc[0-9]{3}", release_id):
        raise ValueError("release-id must use rcNNN form")
    release_dir = resolve_inside(root / "releases" / release_id, root)
    rc_path = resolve_inside(release_dir / "rc.toml", root)
    if not rc_path.is_file():
        raise FileNotFoundError(f"unknown release candidate: {release_id}")
    _require_validation(root, "release")
    rc = load_toml(rc_path)
    if rc.get("status") != "sealed" or not seal_is_valid(rc):
        raise ValueError("release candidate has an invalid seal")
    path = resolve_inside(release_dir / "publication.toml", root)
    if path.exists():
        raise FileExistsError(f"publication record already exists: {path}")
    index = _record_index(root)
    master = _unique_record(index, str(rc.get("master_ref") or ""))
    expected = str(master.get("sha256") or "").removeprefix("sha256:")
    if asset_sha256.removeprefix("sha256:") != expected:
        raise ValueError("published asset SHA-256 does not match the frozen master")
    metadata = load_toml(resolve_inside(Path(str(rc.get("metadata_path") or "")), root, must_exist=True))
    expected_tag = str(metadata.get("github_tag") or "")
    release_owner, release_repo, release_tag, _ = _github_publication_parts(release_url, "release")
    asset_owner, asset_repo, asset_tag, url_asset_name = _github_publication_parts(asset_url, "asset")
    if (release_owner, release_repo, release_tag) != (asset_owner, asset_repo, asset_tag):
        raise ValueError("release and asset URLs must identify the same repository and tag")
    if release_tag != expected_tag:
        raise ValueError("publication URL tag does not match frozen metadata")
    if url_asset_name != asset_name or Path(asset_name).name != asset_name:
        raise ValueError("publication asset name does not match the asset URL")
    staging = resolve_inside(
        Path(str(rc.get("staging_path") or "")), root, must_exist=True
    )
    locations_before = set(
        (root / "assets" / "records" / "locations").glob("al-*.toml")
    )
    _, canonical_location_path = create_asset_record(
        root,
        source=staging,
        sha256=expected,
        size_bytes=int(master.get("size_bytes") or 0),
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
            "human_confirmed": True,
            "published_at": now_iso(),
        }
    )
    try:
        atomic_write_toml(path, seal_record(record))
    except BaseException:
        if canonical_location_path not in locations_before:
            canonical_location_path.unlink(missing_ok=True)
        raise
    return path
