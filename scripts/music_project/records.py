from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION, TEMPLATE_VERSION, TOOL_VERSION
from .io import (
    atomic_write_toml,
    canonical_hash,
    load_toml,
    now_iso,
    parse_key_values,
    relative_path,
    resolve_inside,
    validate_identifier,
)


def common_record(
    record_type: str,
    record_id: str,
    *,
    actor: str,
    status: str = "sealed",
    created_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": record_type,
        "record_id": f"{record_type}:{record_id}",
        "created_at": created_at or now_iso(),
        "created_by": actor,
        "created_with": f"music-project-template {TOOL_VERSION}",
        "status": status,
    }


def seal_record(record: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(record)
    sealed.pop("seal", None)
    sealed["seal"] = {"sealed": True, "content_sha256": canonical_hash(sealed)}
    return sealed


def seal_is_valid(record: dict[str, Any]) -> bool:
    seal = record.get("seal") or {}
    return bool(seal.get("sealed")) and seal.get("content_sha256") == canonical_hash(record)


def _next_id(directory: Path, prefix: str, record_type: str) -> str:
    maximum = 0
    if directory.exists():
        for path in directory.glob(f"{prefix}[0-9]*.toml"):
            try:
                record = load_toml(path)
            except (OSError, ValueError):
                continue
            if record.get("record_type") != record_type:
                continue
            match = re.fullmatch(rf"{re.escape(prefix)}(\d+)", path.stem)
            if match:
                maximum = max(maximum, int(match.group(1)))
    return f"{prefix}{maximum + 1:03d}"


def _replace_template_values(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_template_values(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_template_values(item, replacements) for item in value]
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
    return value


def new_track(root: Path, track_id: str, title: str, actor: str) -> Path:
    validate_identifier(track_id, "track-id")
    template = root / "tracks" / "_template"
    target = root / "tracks" / track_id
    if target.exists():
        raise FileExistsError(f"track already exists: {track_id}")
    shutil.copytree(template, target)
    replacements = {
        "__TRACK_ID__": track_id,
        "__TITLE__": title,
        "__CREATED_AT__": now_iso(),
        "__CREATED_BY__": actor,
    }
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".toml":
            atomic_write_toml(
                path, _replace_template_values(load_toml(path), replacements)
            )
        elif path.suffix.lower() in {".md", ".csv", ".tsv"}:
            text = path.read_text(encoding="utf-8")
            for old, new in replacements.items():
                text = text.replace(old, new)
            path.write_text(text, encoding="utf-8")

    portfolio_path = root / "portfolio.toml"
    portfolio = load_toml(portfolio_path)
    refs = list(portfolio.get("track_refs") or [])
    track_ref = f"track:{track_id}"
    if track_ref not in refs:
        refs.append(track_ref)
    portfolio["track_refs"] = refs
    atomic_write_toml(portfolio_path, portfolio)
    return target


def _track_directory(root: Path, track_id: str) -> Path:
    validate_identifier(track_id, "track-id")
    path = root / "tracks" / track_id
    if not (path / "track.toml").is_file():
        raise FileNotFoundError(f"unknown track: {track_id}")
    return path


def register_generation(
    root: Path,
    *,
    track_id: str,
    actor: str,
    provider: str,
    operation: str,
    occurred_at: str,
    model: str,
    object_id: str,
    plan: str,
    prompt_ref: str,
    lyrics_ref: str,
    terms_snapshot_ref: str,
    parent_refs: list[str],
    input_refs: list[str],
    output_refs: list[str],
    provider_data: list[str],
) -> Path:
    track = _track_directory(root, track_id)
    terms_snapshot_ref, terms_snapshot_sha256 = _policy_snapshot(
        root, provider, terms_snapshot_ref
    )
    record_id = _next_id(track / "generations", "g", "generation")
    record = common_record("generation", record_id, actor=actor)
    record.update(
        {
            "track_ref": f"track:{track_id}",
            "occurred_at": occurred_at or now_iso(),
            "provider": provider,
            "provider_profile_version": _provider_version(root, provider),
            "provider_operation": operation,
            "model": model,
            "provider_object_id": object_id,
            "plan_at_operation": plan,
            "terms_snapshot_ref": terms_snapshot_ref,
            "terms_snapshot_sha256": terms_snapshot_sha256,
            "prompt_ref": prompt_ref,
            "lyrics_ref": lyrics_ref,
            "parent_refs": parent_refs,
            "input_refs": input_refs,
            "output_refs": output_refs,
            "provider_data": parse_key_values(provider_data),
        }
    )
    path = track / "generations" / f"{record_id}.toml"
    atomic_write_toml(path, seal_record(record))
    return path


def register_edit(
    root: Path,
    *,
    track_id: str,
    actor: str,
    provider: str,
    operation: str,
    occurred_at: str,
    parent_refs: list[str],
    input_refs: list[str],
    output_refs: list[str],
    provider_data: list[str],
) -> Path:
    if not parent_refs:
        raise ValueError("an edit requires at least one parent-ref")
    track = _track_directory(root, track_id)
    record_id = _next_id(track / "edits", "e", "edit")
    record = common_record("edit", record_id, actor=actor)
    record.update(
        {
            "track_ref": f"track:{track_id}",
            "occurred_at": occurred_at or now_iso(),
            "provider": provider,
            "provider_profile_version": _provider_version(root, provider),
            "provider_operation": operation,
            "parent_refs": parent_refs,
            "input_refs": input_refs,
            "output_refs": output_refs,
            "provider_data": parse_key_values(provider_data),
        }
    )
    path = track / "edits" / f"{record_id}.toml"
    atomic_write_toml(path, seal_record(record))
    return path


def register_export(
    root: Path,
    *,
    track_id: str,
    actor: str,
    provider: str,
    operation: str,
    occurred_at: str,
    parent_refs: list[str],
    input_refs: list[str],
    output_refs: list[str],
    provider_data: list[str],
) -> Path:
    if not parent_refs:
        raise ValueError("an export requires at least one parent-ref")
    track = _track_directory(root, track_id)
    record_id = _next_id(track / "exports", "x", "export")
    record = common_record("export", record_id, actor=actor)
    record.update(
        {
            "track_ref": f"track:{track_id}",
            "occurred_at": occurred_at or now_iso(),
            "provider": provider,
            "provider_profile_version": _provider_version(root, provider),
            "provider_operation": operation,
            "parent_refs": parent_refs,
            "input_refs": input_refs,
            "output_refs": output_refs,
            "provider_data": parse_key_values(provider_data),
        }
    )
    path = track / "exports" / f"{record_id}.toml"
    atomic_write_toml(path, seal_record(record))
    return path


def register_render(
    root: Path,
    *,
    track_id: str,
    actor: str,
    toolchain: str,
    parent_refs: list[str],
    input_refs: list[str],
    output_refs: list[str],
    notes: str,
) -> Path:
    if not parent_refs:
        raise ValueError("a render requires at least one parent-ref")
    track = _track_directory(root, track_id)
    record_id = _next_id(track / "renders", "r", "render")
    record = common_record("render", record_id, actor=actor)
    record.update(
        {
            "track_ref": f"track:{track_id}",
            "toolchain": toolchain,
            "parent_refs": parent_refs,
            "input_refs": input_refs,
            "output_refs": output_refs,
            "notes": notes,
        }
    )
    path = track / "renders" / f"{record_id}.toml"
    atomic_write_toml(path, seal_record(record))
    return path


def record_review(
    root: Path,
    *,
    track_id: str,
    actor: str,
    subject_ref: str,
    decision: str,
    blind_label: str,
    timestamp_notes: str,
    subject_sha256: str = "",
) -> Path:
    allowed = {"relisten", "shortlist", "selected", "needs_work", "rejected", "approved"}
    if decision not in allowed:
        raise ValueError(f"decision must be one of: {', '.join(sorted(allowed))}")
    track = _track_directory(root, track_id)
    record_id = _next_id(track / "reviews", "lr", "review")
    record = common_record("review", record_id, actor=actor)
    record.update(
        {
            "track_ref": f"track:{track_id}",
            "subject_ref": subject_ref,
            "subject_sha256": subject_sha256.removeprefix("sha256:"),
            "reviewer": actor,
            "blind_label": blind_label,
            "decision": decision,
            "timestamp_notes": timestamp_notes,
            "human_confirmed": True,
        }
    )
    path = track / "reviews" / f"{record_id}.toml"
    atomic_write_toml(path, seal_record(record))
    csv_path = track / "reviews" / "listening.csv"
    with csv_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                record_id,
                subject_ref,
                subject_sha256.removeprefix("sha256:"),
                actor,
                blind_label,
                decision,
                timestamp_notes,
                record["created_at"],
            ]
        )
    return path


def record_rights(
    root: Path,
    *,
    track_id: str,
    actor: str,
    subject_ref: str,
    source_type: str,
    human_status: str,
    private_locator: str,
    evidence_sha256: str,
    public_note: str,
) -> Path:
    allowed = {"confirmed", "needs_review", "missing", "known_unlicensed"}
    if human_status not in allowed:
        raise ValueError(f"human-status must be one of: {', '.join(sorted(allowed))}")
    if human_status == "confirmed":
        if not private_locator:
            raise ValueError("confirmed rights evidence requires private-locator")
        if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256):
            raise ValueError("confirmed rights evidence requires a lowercase SHA-256")
    track = _track_directory(root, track_id)
    record_id = _next_id(track / "rights", "re", "rights_evidence")
    record = common_record("rights_evidence", record_id, actor=actor)
    record.update(
        {
            "track_ref": f"track:{track_id}",
            "subject_ref": subject_ref,
            "source_type": source_type,
            "human_status": human_status,
            "private_locator": private_locator,
            "evidence_sha256": evidence_sha256,
            "public_note": public_note,
            "human_confirmed": True,
        }
    )
    path = track / "rights" / f"{record_id}.toml"
    atomic_write_toml(path, seal_record(record))
    return path


def _provider_version(root: Path, provider: str) -> str:
    validate_identifier(provider, "provider")
    profile = root / "providers" / provider / "profile.toml"
    if not profile.is_file():
        raise FileNotFoundError(f"unknown provider profile: {provider}")
    return str(load_toml(profile).get("profile_version", ""))


def _policy_snapshot(root: Path, provider: str, reference: str) -> tuple[str, str]:
    if not reference:
        return "", ""
    snapshot = resolve_inside(Path(reference), root, must_exist=True)
    snapshot.relative_to((root / "policies" / "platforms" / provider).resolve())
    data = load_toml(snapshot)
    if data.get("provider") != provider:
        raise ValueError("terms snapshot provider does not match generation provider")
    from .io import sha256_file

    return relative_path(snapshot, root), sha256_file(snapshot)


def template_version() -> str:
    return TEMPLATE_VERSION
