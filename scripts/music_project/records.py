from __future__ import annotations

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
    relative_path,
    resolve_inside,
    validate_identifier,
)


def common_record(
    record_type: str,
    record_id: str,
    *,
    actor: str = "",
    submitted_by: dict[str, str] | None = None,
    status: str = "sealed",
    created_at: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": record_type,
        "record_id": f"{record_type}:{record_id}",
        "created_at": created_at or now_iso(),
        "created_with": f"music-project-template {TOOL_VERSION}",
        "status": status,
    }
    if submitted_by is not None:
        record["submitted_by"] = dict(submitted_by)
    elif actor:
        record["created_by"] = actor
    return record


def seal_record(record: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(record)
    sealed.pop("seal", None)
    sealed["seal"] = {"sealed": True, "content_sha256": canonical_hash(sealed)}
    return sealed


def seal_is_valid(record: dict[str, Any]) -> bool:
    seal = record.get("seal") or {}
    return bool(isinstance(seal, dict) and seal.get("sealed")) and seal.get(
        "content_sha256"
    ) == canonical_hash(record)


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
