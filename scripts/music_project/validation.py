from __future__ import annotations

import json
import re
import subprocess
import tomllib
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from . import SCHEMA_VERSION, TEMPLATE_VERSION, TOOL_VERSION
from .io import canonical_hash, load_toml, resolve_inside, sha256_file


RECORD_TYPES = {
    "portfolio",
    "track",
    "lyrics",
    "prompt",
    "generation",
    "edit",
    "export",
    "render",
    "asset",
    "asset_location",
    "review",
    "rights_evidence",
    "release_candidate",
    "publication",
}
EVENT_TYPES = {"generation", "edit", "export", "render"}
EVIDENCE_EVENT_TYPES = {
    *EVENT_TYPES,
    "review",
    "rights_evidence",
    "release_candidate",
}
WORKFLOW_STATES = {
    "briefing",
    "generating",
    "reviewing",
    "selected",
    "postproduction",
    "post_review",
    "release_candidate",
    "frozen",
    "released",
    "abandoned",
}
LEVELS = {"minimal": 0, "post": 1, "release": 2}
REFERENCE_FIELD_TYPES = {
    "asset_ref": {"asset"},
    "canonical_location_ref": {"asset_location"},
    "input_refs": {"asset"},
    "listening_gate_refs": {"review"},
    "lyrics_ref": {"lyrics"},
    "master_ref": {"asset"},
    "output_refs": {"asset"},
    "parent_refs": EVENT_TYPES,
    "prompt_ref": {"prompt"},
    "release_candidate_ref": {"release_candidate"},
    "release_refs": {"release_candidate", "publication"},
    "rights_refs": {"rights_evidence"},
    "subject_ref": {"track", *EVENT_TYPES, "asset", "release_candidate"},
    "track_ref": {"track"},
    "track_refs": {"track"},
}
PATH_REFERENCE_FIELDS = {"policy_snapshot_refs", "terms_snapshot_ref"}
FORBIDDEN_CREDENTIAL_KEYS = {
    "api_key",
    "access_token",
    "authorization",
    "cookie",
    "password",
    "refresh_token",
    "secret",
    "session_token",
}
IMMUTABLE_RECORD_TYPES = {
    "generation",
    "edit",
    "export",
    "render",
    "asset",
    "asset_location",
    "review",
    "rights_evidence",
    "release_candidate",
    "publication",
}
RETENTION_MODES = {"metadata_only", "timed_local", "external_archive"}


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    path: str
    message: str


def _table(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def record_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    candidates = [root / "portfolio.toml"]
    candidates.extend((root / "tracks").glob("*/**/*.toml"))
    candidates.extend((root / "assets" / "records").glob("**/*.toml"))
    candidates.extend((root / "releases").glob("rc*/rc.toml"))
    candidates.extend((root / "releases").glob("rc*/publication.toml"))
    for path in candidates:
        relative = path.relative_to(root).as_posix() if path.exists() else ""
        if not path.is_file() or "/_template/" in f"/{relative}":
            continue
        paths.append(path)
    return sorted(set(paths))


def load_records(root: Path) -> tuple[list[tuple[Path, dict[str, Any]]], list[Issue]]:
    records: list[tuple[Path, dict[str, Any]]] = []
    issues: list[Issue] = []
    for path in record_paths(root):
        relative = path.relative_to(root).as_posix()
        try:
            record = load_toml(path)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            issues.append(Issue("error", "toml_parse", relative, str(exc)))
            continue
        records.append((path, record))
    return records, issues


def _scope(record: dict[str, Any]) -> str:
    if record.get("record_type") in {"portfolio", "asset", "asset_location"}:
        return "global"
    return str(record.get("track_ref") or record.get("record_id") or "global")


def _registry_key(record: dict[str, Any]) -> str:
    return f"{_scope(record)}|{record.get('record_id', '')}"


def _resolve_ref(
    reference: str, record: dict[str, Any], registry: dict[str, tuple[Path, dict[str, Any]]]
) -> tuple[Path, dict[str, Any]] | None:
    local_key = f"{_scope(record)}|{reference}"
    if local_key in registry:
        return registry[local_key]
    global_key = f"global|{reference}"
    if global_key in registry:
        return registry[global_key]
    if _scope(record) != "global":
        return None
    matches = [item for key, item in registry.items() if key.endswith(f"|{reference}")]
    return matches[0] if len(matches) == 1 else None


def _record_references(record: dict[str, Any]) -> Iterable[tuple[str, str]]:
    for key, value in record.items():
        if key in {"record_id", "evidence_ref"} or key in PATH_REFERENCE_FIELDS:
            continue
        values: list[str] = []
        if key.endswith("_ref") and isinstance(value, str):
            values = [value]
        elif key.endswith("_refs") and isinstance(value, list):
            values = [item for item in value if isinstance(item, str)]
        for reference in values:
            if reference:
                yield key, reference


def _path_values(value: Any, prefix: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if key.endswith("_path") and isinstance(item, str) and item:
                yield full_key, item
            else:
                yield from _path_values(item, full_key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _path_values(item, f"{prefix}[{index}]")


def _forbidden_keys(value: Any, prefix: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            full_key = f"{prefix}.{key}" if prefix else key
            normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
            if any(
                normalized == sensitive or normalized.endswith(f"_{sensitive}")
                for sensitive in FORBIDDEN_CREDENTIAL_KEYS
            ):
                yield full_key
            yield from _forbidden_keys(item, full_key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _forbidden_keys(item, f"{prefix}[{index}]")


def _provider_profiles(root: Path) -> tuple[dict[str, dict[str, Any]], list[Issue]]:
    profiles: dict[str, dict[str, Any]] = {}
    issues: list[Issue] = []
    for path in (root / "providers").glob("*/profile.toml"):
        relative = path.relative_to(root).as_posix()
        try:
            profile = load_toml(path)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            issues.append(Issue("error", "provider_profile", relative, str(exc)))
            continue
        required = ("profile_version", "provider", "mode", "generation_operations")
        missing = [field for field in required if profile.get(field) in (None, "", [])]
        if missing:
            issues.append(
                Issue(
                    "error",
                    "provider_profile",
                    relative,
                    f"missing provider profile fields: {', '.join(missing)}",
                )
            )
            continue
        if profile.get("unofficial_automation_allowed") is not False:
            issues.append(
                Issue(
                    "error",
                    "provider_profile",
                    relative,
                    "unofficial_automation_allowed must be false",
                )
            )
        profiles[str(profile.get("provider") or path.parent.name)] = profile
    return profiles, issues


def _validate_config(root: Path) -> tuple[dict[str, Any], list[Issue]]:
    path = root / "music.toml"
    if not path.is_file():
        return {}, []
    try:
        config = load_toml(path)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, [Issue("error", "config_parse", "music.toml", str(exc))]
    issues: list[Issue] = []
    expected_versions = {
        "schema_version": SCHEMA_VERSION,
        "template_version": TEMPLATE_VERSION,
        "tool_version": TOOL_VERSION,
    }
    for field, expected in expected_versions.items():
        if config.get(field) != expected:
            issues.append(
                Issue(
                    "error",
                    "config_version",
                    "music.toml",
                    f"expected {field}={expected!r}, got {config.get(field)!r}",
                )
            )
    normalized_config = dict(config)
    tables = {
        "project": "config_project",
        "candidate_retention": "config_retention",
        "storage": "config_storage",
        "import_limits": "config_import_limits",
        "external_tools": "config_external_tools",
        "licenses": "config_licenses",
    }
    for field, code in tables.items():
        value = config.get(field)
        if not isinstance(value, dict):
            issues.append(Issue("error", code, "music.toml", f"{field} must be a TOML table"))
            normalized_config[field] = {}
    project = _table(normalized_config.get("project"))
    for field in ("project_id", "title", "default_provider", "timezone"):
        if not isinstance(project.get(field), str) or not project.get(field):
            issues.append(Issue("error", "config_project", "music.toml", f"missing project.{field}"))
    retention = _table(normalized_config.get("candidate_retention"))
    mode = retention.get("mode")
    if mode not in RETENTION_MODES:
        issues.append(Issue("error", "config_retention", "music.toml", f"invalid retention mode: {mode}"))
    if mode == "timed_local" and (
        not isinstance(retention.get("days"), int) or retention.get("days", -1) < 0
    ):
        issues.append(Issue("error", "config_retention", "music.toml", "timed_local requires non-negative integer days"))
    if mode == "external_archive" and not retention.get("archive_locator"):
        issues.append(Issue("error", "config_retention", "music.toml", "external_archive requires archive_locator"))
    storage = _table(normalized_config.get("storage"))
    if storage.get("public_release_canonical") != "github_release":
        issues.append(Issue("error", "config_storage", "music.toml", "public_release_canonical must be github_release"))
    limits = _table(normalized_config.get("import_limits"))
    for field in ("max_entries", "max_file_bytes", "max_total_bytes", "max_compression_ratio"):
        value = limits.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            issues.append(Issue("error", "config_import_limits", "music.toml", f"invalid import_limits.{field}"))
    return normalized_config, issues


def _check_lfs_attribute(root: Path, path: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["git", "check-attr", "filter", "--", path],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return False, result.stderr.strip() or "git check-attr failed"
    return result.stdout.rstrip().endswith(": lfs"), result.stdout.strip()


def validate(root: Path, level: str) -> dict[str, Any]:
    if level not in LEVELS:
        raise ValueError(f"level must be one of: {', '.join(sorted(LEVELS))}")
    issues: list[Issue] = []
    for required_path in ("music.toml", "portfolio.toml"):
        if not (root / required_path).is_file():
            issues.append(
                Issue(
                    "error",
                    "project_file",
                    required_path,
                    f"missing required project file: {required_path}",
                )
            )
    config, config_issues = _validate_config(root)
    issues.extend(config_issues)
    records, load_issues = load_records(root)
    issues.extend(load_issues)
    registry: dict[str, tuple[Path, dict[str, Any]]] = {}
    profiles, profile_issues = _provider_profiles(root)
    issues.extend(profile_issues)
    default_provider = str(_table(config.get("project")).get("default_provider") or "")
    if default_provider and default_provider not in profiles:
        issues.append(Issue("error", "config_provider", "music.toml", f"unknown default provider: {default_provider}"))

    for path, record in records:
        relative = path.relative_to(root).as_posix()
        required = ("schema_version", "record_type", "record_id", "created_with", "status")
        for field in required:
            if field not in record:
                issues.append(Issue("error", "required", relative, f"missing {field}"))
        if record.get("schema_version") != SCHEMA_VERSION:
            issues.append(
                Issue(
                    "error",
                    "schema_version",
                    relative,
                    f"expected {SCHEMA_VERSION}, got {record.get('schema_version')!r}",
                )
            )
        record_type = record.get("record_type")
        if record_type not in RECORD_TYPES:
            issues.append(Issue("error", "record_type", relative, f"unknown record type: {record_type}"))
        key = _registry_key(record)
        if key in registry:
            issues.append(Issue("error", "duplicate_id", relative, f"duplicate scoped record ID: {record.get('record_id')}"))
        else:
            registry[key] = (path, record)

        for forbidden_key in _forbidden_keys(record):
            issues.append(
                Issue(
                    "error",
                    "credential_field",
                    relative,
                    f"credentials must not be stored in canonical records: {forbidden_key}",
                )
            )

        for field, value in record.items():
            if field in PATH_REFERENCE_FIELDS:
                continue
            if field.endswith("_ref") and not isinstance(value, str):
                issues.append(
                    Issue("error", "malformed_ref", relative, f"{field} must be a string")
                )
            elif field.endswith("_refs") and (
                not isinstance(value, list)
                or any(not isinstance(item, str) for item in value)
            ):
                issues.append(
                    Issue(
                        "error",
                        "malformed_ref",
                        relative,
                        f"{field} must be a list of strings",
                    )
                )

        if record_type in IMMUTABLE_RECORD_TYPES and record.get("status") != "sealed":
            issues.append(Issue("error", "seal_required", relative, f"{record_type} records must be sealed"))
        if record_type in IMMUTABLE_RECORD_TYPES or record.get("status") == "sealed" or record.get("seal"):
            seal = _table(record.get("seal"))
            if not seal.get("sealed") or seal.get("content_sha256") != canonical_hash(record):
                issues.append(Issue("error", "seal_mismatch", relative, "sealed record content hash does not match"))

        if record_type == "track" and record.get("workflow_state") not in WORKFLOW_STATES:
            issues.append(Issue("error", "workflow_state", relative, f"invalid workflow state: {record.get('workflow_state')}"))

        if record_type == "asset":
            digest = str(record.get("sha256") or "")
            expected_id = f"asset:a-{digest}"
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                issues.append(Issue("error", "asset_hash", relative, "asset SHA-256 is malformed"))
            elif record.get("record_id") != expected_id:
                issues.append(
                    Issue(
                        "error",
                        "asset_identity",
                        relative,
                        "asset record ID must contain the complete SHA-256",
                    )
                )

        if record_type in EVIDENCE_EVENT_TYPES:
            submission_id = record.get("submission_id")
            try:
                normalized_submission_id = str(uuid.UUID(str(submission_id)))
            except (ValueError, AttributeError):
                normalized_submission_id = ""
            if str(submission_id).lower() != normalized_submission_id:
                issues.append(Issue("error", "submission_id", relative, "event submission_id must be a canonical UUID"))
            if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("envelope_digest") or "")):
                issues.append(Issue("error", "envelope_digest", relative, "event envelope_digest is malformed"))
            submitted_by = _table(record.get("submitted_by"))
            if submitted_by.get("type") not in {"human", "agent", "automation"} or not submitted_by.get("id"):
                issues.append(Issue("error", "submitted_by", relative, "event submitted_by is malformed"))
            responsibility = {
                "review": "reviewed_by",
                "rights_evidence": "assessed_by",
                "release_candidate": "confirmed_by",
            }.get(str(record_type))
            if responsibility:
                responsible = _table(record.get(responsibility))
                if responsible.get("type") != "human" or not responsible.get("id"):
                    issues.append(Issue("error", responsibility, relative, f"{responsibility} must identify a human"))

        if record_type == "rights_evidence" and record.get("human_status") == "confirmed":
            evidence_hash = str(record.get("evidence_sha256") or "")
            if not record.get("evidence_ref") or not re.fullmatch(r"[0-9a-f]{64}", evidence_hash):
                issues.append(
                    Issue(
                        "error",
                        "rights_evidence",
                        relative,
                        "confirmed rights evidence requires an opaque safe reference and lowercase SHA-256",
                    )
                )

        for key_name, raw_path in _path_values(record):
            try:
                resolved = resolve_inside(Path(raw_path), root)
            except ValueError as exc:
                issues.append(Issue("error", "path_escape", relative, f"{key_name}: {exc}"))
                continue
            if key_name.endswith("text_path") and not resolved.is_file():
                issues.append(Issue("error", "missing_text", relative, f"missing file: {raw_path}"))

        terms_snapshot_ref = str(record.get("terms_snapshot_ref") or "")
        if terms_snapshot_ref:
            try:
                snapshot = resolve_inside(Path(terms_snapshot_ref), root, must_exist=True)
                if not snapshot.is_file():
                    raise ValueError("not a file")
                snapshot_relative = snapshot.relative_to(root.resolve()).parts
                provider = str(record.get("provider") or "")
                if snapshot_relative[:3] != ("policies", "platforms", provider):
                    raise ValueError("snapshot path does not match the record provider")
                snapshot_record = load_toml(snapshot)
                if snapshot_record.get("provider") != provider:
                    raise ValueError("snapshot provider does not match the record provider")
                if record.get("terms_snapshot_sha256") != sha256_file(snapshot):
                    raise ValueError("snapshot SHA-256 does not match")
                if terms_snapshot_ref != snapshot.relative_to(root.resolve()).as_posix():
                    raise ValueError("snapshot reference is not a canonical repository-relative path")
            except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
                issues.append(
                    Issue(
                        "error",
                        "terms_snapshot",
                        relative,
                        f"invalid terms snapshot {terms_snapshot_ref}: {exc}",
                    )
                )

        if record_type in {"generation", "edit", "export"}:
            provider = str(record.get("provider") or "")
            profile = profiles.get(provider)
            if not profile:
                issues.append(Issue("error", "provider", relative, f"unknown provider profile: {provider}"))
            else:
                profile_version = str(profile.get("profile_version") or "")
                if record.get("provider_profile_version") != profile_version:
                    issues.append(Issue("error", "provider_version", relative, f"expected provider profile {profile_version}"))
                required_fields = _table(
                    _table(profile.get("required")).get(record_type)
                ).get("fields") or []
                for field in required_fields:
                    if record.get(field) in (None, "", []):
                        issues.append(Issue("error", "provider_required", relative, f"missing provider field: {field}"))
                operation_key = {
                    "generation": "generation_operations",
                    "edit": "edit_operations",
                    "export": "export_operations",
                }[record_type]
                if record.get("provider_operation") not in (profile.get(operation_key) or []):
                    issues.append(Issue("error", "provider_operation", relative, f"unsupported operation: {record.get('provider_operation')}"))
                allowed_data = set(_table(profile.get("provider_data")).get("allowed_keys") or [])
                unknown = set(_table(record.get("provider_data"))) - allowed_data
                if unknown:
                    issues.append(Issue("error", "provider_data", relative, f"unknown provider_data keys: {', '.join(sorted(unknown))}"))

    portfolios = [record for _, record in records if record.get("record_type") == "portfolio"]
    if len(portfolios) != 1 or portfolios[0].get("record_id") != "portfolio:main":
        issues.append(
            Issue(
                "error",
                "portfolio_singleton",
                "portfolio.toml",
                "project requires exactly one portfolio:main record",
            )
        )

    for path, record in records:
        relative = path.relative_to(root).as_posix()
        for field, reference in _record_references(record):
            match = re.fullmatch(r"([a-z_]+):([A-Za-z0-9][A-Za-z0-9._-]*)", reference)
            allowed_types = REFERENCE_FIELD_TYPES.get(field)
            if not match or match.group(1) not in RECORD_TYPES:
                issues.append(Issue("error", "malformed_ref", relative, f"{field} is not a typed reference: {reference}"))
                continue
            if allowed_types is None or match.group(1) not in allowed_types:
                issues.append(Issue("error", "reference_type", relative, f"invalid reference type for {field}: {reference}"))
                continue
            if _resolve_ref(reference, record, registry) is None:
                issues.append(Issue("error", "missing_ref", relative, f"{field} points to missing or ambiguous {reference}"))

    rights_by_subject: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for _, record in records:
        if record.get("record_type") == "rights_evidence":
            rights_by_subject.setdefault(
                (str(record.get("track_ref") or ""), str(record.get("subject_ref") or "")),
                [],
            ).append(record)
    for path, record in records:
        if record.get("record_type") not in {"generation", "edit"}:
            continue
        profile = profiles.get(str(record.get("provider") or "")) or {}
        rules = _table(profile.get("rights"))
        provider_data = _table(record.get("provider_data"))
        requires_voice = bool(rules.get("voice_requires_evidence")) and any(
            provider_data.get(key) not in (None, "", False) for key in ("voice", "persona")
        )
        requires_input = bool(rules.get("uploaded_audio_requires_evidence")) and bool(
            record.get("input_refs")
        )
        if not (requires_voice or requires_input):
            continue
        evidence = rights_by_subject.get(
            (str(record.get("track_ref") or ""), str(record.get("record_id") or "")),
            [],
        )
        covered = any(
            item.get("human_status") == "confirmed"
            and _table(item.get("assessed_by")).get("type") == "human"
            and item.get("status") == "sealed"
            and _table(item.get("seal")).get("content_sha256") == canonical_hash(item)
            for item in evidence
        )
        if not covered:
            issues.append(
                Issue(
                    "warning",
                    "provider_rights",
                    path.relative_to(root).as_posix(),
                    "provider profile requires confirmed rights evidence for this event",
                )
            )

    graph: dict[str, list[str]] = {}
    for path, record in records:
        if record.get("record_type") not in EVENT_TYPES:
            continue
        node = _registry_key(record)
        parents: list[str] = []
        for reference in record.get("parent_refs") or []:
            resolved = _resolve_ref(reference, record, registry)
            if resolved and resolved[1].get("record_type") in EVENT_TYPES:
                parents.append(_registry_key(resolved[1]))
        graph[node] = parents

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            path = registry[node][0].relative_to(root).as_posix()
            issues.append(Issue("error", "dag_cycle", path, "event lineage contains a cycle"))
            return
        if node in visited:
            return
        visiting.add(node)
        for parent in graph.get(node, []):
            visit(parent)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)

    if LEVELS[level] >= LEVELS["post"]:
        github_release_asset_refs = {
            str(item.get("asset_ref") or "")
            for _, item in records
            if item.get("record_type") == "asset_location"
            and item.get("storage") == "github_release"
            and str(item.get("archived_locator") or "").startswith(
                "https://github.com/"
            )
            and item.get("status") == "sealed"
            and _table(item.get("seal")).get("content_sha256")
            == canonical_hash(item)
        }
        for path, record in records:
            if record.get("record_type") not in {"lyrics", "prompt", "asset_location"}:
                continue
            relative = path.relative_to(root).as_posix()
            if record.get("record_type") in {"lyrics", "prompt"}:
                text_path = str(record.get("text_path") or "")
                expected = str(record.get("sha256") or "")
                if not text_path or not expected:
                    issues.append(Issue("error", "text_hash", relative, "post validation requires text_path and sha256"))
                else:
                    try:
                        actual = sha256_file(resolve_inside(Path(text_path), root, must_exist=True))
                        if actual != expected.removeprefix("sha256:"):
                            issues.append(Issue("error", "text_hash", relative, "text SHA-256 mismatch"))
                    except (OSError, ValueError) as exc:
                        issues.append(Issue("error", "text_hash", relative, str(exc)))
                continue

            storage = record.get("storage")
            local_path = str(record.get("local_path") or "")
            asset = _resolve_ref(str(record.get("asset_ref") or ""), record, registry)
            if not asset or asset[1].get("record_type") != "asset":
                issues.append(
                    Issue(
                        "error",
                        "asset_location",
                        relative,
                        "asset_location must reference an asset",
                    )
                )
                continue
            expected = str(asset[1].get("sha256") or "").removeprefix("sha256:")
            if storage in {"git_lfs", "release_staging"}:
                if not local_path:
                    issues.append(Issue("error", "asset_path", relative, "local asset storage requires local_path"))
                    continue
                try:
                    asset_path = resolve_inside(Path(local_path), root, must_exist=True)
                    actual = sha256_file(asset_path)
                except (OSError, ValueError) as exc:
                    if (
                        storage == "release_staging"
                        and str(record.get("asset_ref") or "")
                        in github_release_asset_refs
                    ):
                        continue
                    issues.append(Issue("error", "asset_path", relative, str(exc)))
                    continue
                if actual != expected:
                    issues.append(Issue("error", "asset_hash", relative, "asset SHA-256 mismatch"))
                if storage == "git_lfs":
                    is_lfs, detail = _check_lfs_attribute(root, local_path)
                    if not is_lfs:
                        issues.append(Issue("error", "lfs_policy", relative, f"asset is not covered by Git LFS: {detail}"))
            elif storage == "github_release":
                locator = str(record.get("archived_locator") or "")
                if not locator.startswith("https://github.com/"):
                    issues.append(
                        Issue(
                            "error",
                            "asset_location",
                            relative,
                            "github_release requires a GitHub HTTPS locator",
                        )
                    )
            elif storage == "external_archive" and not record.get("archived_locator"):
                issues.append(
                    Issue(
                        "error",
                        "asset_location",
                        relative,
                        "external_archive requires archived_locator",
                    )
                )
            elif storage not in {"metadata_only", "external_archive"}:
                issues.append(
                    Issue(
                        "error",
                        "asset_location",
                        relative,
                        f"unknown storage: {storage}",
                    )
                )

    if LEVELS[level] >= LEVELS["release"]:
        _validate_releases(root, records, registry, issues)

    errors = [asdict(issue) for issue in issues if issue.severity == "error"]
    warnings = [asdict(issue) for issue in issues if issue.severity == "warning"]
    return {
        "status": "PASS" if not errors else "FAIL",
        "level": level,
        "records_checked": len(records),
        "errors": errors,
        "warnings": warnings,
        "human_gates": [
            "artistic_quality",
            "full_listen",
            "voice_identity",
            "musical_similarity",
            "rights_truth",
            "formal_publication",
        ],
    }


def _validate_releases(
    root: Path,
    records: list[tuple[Path, dict[str, Any]]],
    registry: dict[str, tuple[Path, dict[str, Any]]],
    issues: list[Issue],
) -> None:
    releases = [(path, record) for path, record in records if record.get("record_type") == "release_candidate"]
    if not releases:
        issues.append(Issue("error", "release_missing", "releases", "no release candidate records found"))
        return
    for path, record in releases:
        relative = path.relative_to(root).as_posix()
        frozen = _table(record.get("frozen"))
        if not frozen.get("value"):
            issues.append(Issue("error", "release_not_frozen", relative, "release candidate is not frozen"))
        master_digest = str(record.get("master_sha256") or "").removeprefix("sha256:")
        master_path = str(record.get("master_path") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", master_digest):
            issues.append(Issue("error", "master_identity", relative, "master_sha256 is malformed"))
        else:
            try:
                if sha256_file(resolve_inside(Path(master_path), root, must_exist=True)) != master_digest:
                    raise ValueError("master SHA-256 mismatch")
            except (OSError, ValueError) as exc:
                issues.append(Issue("error", "master_identity", relative, str(exc)))
        raw_review_refs = record.get("listening_gate_refs")
        review_refs = (
            raw_review_refs
            if isinstance(raw_review_refs, list)
            and all(isinstance(item, str) for item in raw_review_refs)
            else []
        )
        if raw_review_refs != review_refs:
            issues.append(Issue("error", "listening_gate", relative, "listening_gate_refs must be a list of strings"))
        if not review_refs:
            issues.append(Issue("error", "listening_gate", relative, "missing listening gate"))
        for reference in review_refs:
            review = _resolve_ref(reference, record, registry)
            if (
                not review
                or review[1].get("record_type") != "review"
                or review[1].get("status") != "sealed"
                or _table(review[1].get("seal")).get("content_sha256") != canonical_hash(review[1])
                or review[1].get("decision") not in {"approved", "selected"}
                or _table(review[1].get("reviewed_by")).get("type") != "human"
                or str(review[1].get("subject_sha256") or "").removeprefix("sha256:")
                != master_digest
            ):
                issues.append(Issue("error", "listening_gate", relative, f"invalid listening gate: {reference}"))
        raw_rights_refs = record.get("rights_refs")
        rights_refs = (
            raw_rights_refs
            if isinstance(raw_rights_refs, list)
            and all(isinstance(item, str) for item in raw_rights_refs)
            else []
        )
        if raw_rights_refs != rights_refs:
            issues.append(Issue("error", "rights_gate", relative, "rights_refs must be a list of strings"))
        override_valid = (
            record.get("gate_status") == "PASS_WITH_OVERRIDE"
            and bool(record.get("override_reason"))
            and _table(record.get("confirmed_by")).get("type") == "human"
        )
        if not rights_refs and not override_valid:
            issues.append(Issue("error", "rights_gate", relative, "missing rights evidence and no valid override"))
        track_blockers = [
            item
            for _, item in records
            if item.get("track_ref") == record.get("track_ref")
            and item.get("record_type") == "rights_evidence"
            and item.get("human_status") == "known_unlicensed"
        ]
        if track_blockers:
            issues.append(
                Issue(
                    "error",
                    "known_unlicensed",
                    relative,
                    "track contains non-overridable known_unlicensed evidence",
                )
            )
        for reference in rights_refs:
            evidence = _resolve_ref(reference, record, registry)
            if (
                not evidence
                or evidence[1].get("record_type") != "rights_evidence"
                or evidence[1].get("status") != "sealed"
                or _table(evidence[1].get("seal")).get("content_sha256") != canonical_hash(evidence[1])
                or _table(evidence[1].get("assessed_by")).get("type") != "human"
            ):
                issues.append(Issue("error", "rights_gate", relative, f"invalid rights evidence: {reference}"))
                continue
            human_status = evidence[1].get("human_status")
            if human_status == "known_unlicensed":
                issues.append(Issue("error", "known_unlicensed", relative, f"non-overridable rights blocker: {reference}"))
            elif human_status != "confirmed" and not override_valid:
                issues.append(Issue("error", "rights_gate", relative, f"unconfirmed rights evidence: {reference}"))
        raw_frozen_files = _table(record.get("output_binding")).get("files")
        frozen_files = raw_frozen_files if isinstance(raw_frozen_files, list) else []
        if raw_frozen_files != frozen_files:
            issues.append(Issue("error", "frozen_file", relative, "frozen_files must be a list of tables"))
        frozen_by_kind: dict[str, list[dict[str, Any]]] = {}
        for item in frozen_files:
            if not isinstance(item, dict):
                issues.append(Issue("error", "frozen_file", relative, "frozen_files must contain tables"))
                continue
            kind = str(item.get("kind") or "")
            frozen_by_kind.setdefault(kind, []).append(item)
            frozen_path = str(item.get("path") or "")
            expected_hash = str(item.get("sha256") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                issues.append(Issue("error", "frozen_file", relative, f"invalid frozen hash: {frozen_path}"))
                continue
            try:
                actual_hash = sha256_file(
                    resolve_inside(Path(frozen_path), root, must_exist=True)
                )
            except (OSError, ValueError) as exc:
                issues.append(Issue("error", "frozen_file", relative, f"{frozen_path}: {exc}"))
                continue
            if actual_hash != expected_hash:
                issues.append(Issue("error", "frozen_file", relative, f"frozen hash mismatch: {frozen_path}"))

        required_frozen = {
            "master": str(record.get("master_path") or ""),
            "metadata": str(record.get("metadata_path") or ""),
            "gate": str(record.get("gate_path") or ""),
            "checksums": str(record.get("checksums_path") or ""),
            "analysis": str(record.get("analysis_path") or ""),
        }
        for kind, expected_path in required_frozen.items():
            paths = [str(item.get("path") or "") for item in frozen_by_kind.get(kind, [])]
            if paths != [expected_path]:
                issues.append(Issue("error", "frozen_file", relative, f"missing or inconsistent frozen {kind}"))
        raw_policy_snapshots = record.get("policy_snapshots")
        policy_snapshots = raw_policy_snapshots if isinstance(raw_policy_snapshots, list) else []
        if raw_policy_snapshots != policy_snapshots:
            issues.append(Issue("error", "policy_snapshot", relative, "policy_snapshots must be a list of tables"))
        policy_paths = [str(item.get("path") or "") for item in policy_snapshots if isinstance(item, dict)]
        raw_policy_refs = record.get("policy_snapshot_refs")
        policy_refs = raw_policy_refs if isinstance(raw_policy_refs, list) else []
        if policy_paths != policy_refs:
            issues.append(Issue("error", "policy_snapshot", relative, "frozen policy snapshots are inconsistent"))
        policy_providers: set[str] = set()
        for item in policy_snapshots:
            if not isinstance(item, dict):
                issues.append(Issue("error", "policy_snapshot", relative, "policy_snapshots must contain tables"))
                continue
            provider = str(item.get("provider") or "")
            policy_path = str(item.get("path") or "")
            try:
                resolved_policy = resolve_inside(Path(policy_path), root, must_exist=True)
                policy_relative = resolved_policy.relative_to(root.resolve()).parts
                policy = load_toml(resolved_policy)
                if (
                    len(policy_relative) < 4
                    or policy_relative[:2] != ("policies", "platforms")
                    or policy_relative[2] != provider
                    or policy.get("provider") != provider
                    or not all(policy.get(field) for field in ("snapshot_id", "provider", "retrieved_at"))
                ):
                    raise ValueError("policy snapshot identity is incomplete or mismatched")
                if sha256_file(resolved_policy) != str(item.get("sha256") or ""):
                    raise ValueError("policy snapshot SHA-256 mismatch")
                policy_providers.add(provider)
            except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
                issues.append(Issue("error", "policy_snapshot", relative, f"invalid policy snapshot: {exc}"))
        track_providers = {
            str(item.get("provider") or "")
            for _, item in records
            if item.get("track_ref") == record.get("track_ref")
            and item.get("record_type") in {"generation", "edit", "export"}
            and item.get("provider")
        }
        missing_policy_providers = sorted(track_providers - policy_providers)
        if missing_policy_providers:
            issues.append(
                Issue(
                    "error",
                    "policy_snapshot",
                    relative,
                    "missing frozen provider policy snapshots: "
                    + ", ".join(missing_policy_providers),
                )
            )
        frozen_policy_identities = {
            (str(item.get("path") or ""), str(item.get("sha256") or ""))
            for item in policy_snapshots
            if isinstance(item, dict)
        }
        required_generation_snapshots = {
            (
                str(item.get("terms_snapshot_ref") or ""),
                str(item.get("terms_snapshot_sha256") or ""),
            )
            for _, item in records
            if item.get("track_ref") == record.get("track_ref")
            and item.get("record_type") == "generation"
            and item.get("terms_snapshot_ref")
        }
        if required_generation_snapshots - frozen_policy_identities:
            issues.append(
                Issue(
                    "error",
                    "policy_snapshot",
                    relative,
                    "release does not freeze every exact generation policy snapshot",
                )
            )

        analysis_path = str(record.get("analysis_path") or "")
        try:
            analysis = json.loads(
                resolve_inside(Path(analysis_path), root, must_exist=True).read_text(
                    encoding="utf-8"
                )
            )
            if not isinstance(analysis, dict):
                raise ValueError("analysis root must be an object")
            probe = _table(analysis.get("probe"))
            streams = probe.get("streams") or []
            if not isinstance(streams, list):
                raise ValueError("analysis streams must be a list")
            audio_streams = [
                stream
                for stream in streams
                if isinstance(stream, dict) and stream.get("codec_type") == "audio"
            ]
            duration = float(_table(probe.get("format")).get("duration") or 0)
            if (
                analysis.get("source_sha256") != master_digest
                or not analysis.get("measurement_only")
                or analysis.get("quality_conclusion") is not None
                or not audio_streams
                or duration <= 0
            ):
                raise ValueError("analysis is not a hash-bound positive-duration audio measurement")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            issues.append(Issue("error", "audio_analysis", relative, str(exc)))

        gate_path = str(record.get("gate_path") or "")
        try:
            gate = load_toml(resolve_inside(Path(gate_path), root, must_exist=True))
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            issues.append(Issue("error", "release_gate", relative, f"invalid frozen gate: {exc}"))
        else:
            expected_gate_values = {
                "listening_gate_refs": review_refs,
                "rights_refs": rights_refs,
                "policy_snapshot_refs": policy_refs,
            }
            for field, expected_value in expected_gate_values.items():
                if gate.get(field) != expected_value:
                    issues.append(Issue("error", "release_gate", relative, f"gate {field} does not match release candidate"))
            expected_status = str(record.get("gate_status") or "")
            if gate.get("status") != expected_status:
                issues.append(Issue("error", "release_gate", relative, "gate status does not match release candidate"))
            if expected_status == "PASS_WITH_OVERRIDE":
                if gate.get("override_reason") != record.get("override_reason"):
                    issues.append(Issue("error", "release_gate", relative, "gate override_reason does not match release candidate"))

        checksums_path = str(record.get("checksums_path") or "")
        try:
            checksums = resolve_inside(Path(checksums_path), root, must_exist=True)
        except (OSError, ValueError) as exc:
            issues.append(Issue("error", "checksums", relative, str(exc)))
            continue
        checksum_entries: dict[str, str] = {}
        malformed_checksum = False
        for line in checksums.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            parts = line.split("  ", 1)
            if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]) or not parts[1]:
                malformed_checksum = True
                break
            checksum_entries[parts[1]] = parts[0]
        if malformed_checksum:
            issues.append(Issue("error", "checksums", relative, "checksums file is malformed"))
        else:
            expected_name = f"master/{Path(master_path).name}"
            if checksum_entries.get(expected_name) != master_digest:
                issues.append(Issue("error", "checksums", relative, "master checksum entry is missing or incorrect"))


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"{report['status']} level={report['level']} records={report['records_checked']}",
    ]
    for issue in report["errors"]:
        lines.append(f"ERROR {issue['code']} {issue['path']}: {issue['message']}")
    for issue in report["warnings"]:
        lines.append(f"WARN  {issue['code']} {issue['path']}: {issue['message']}")
    return "\n".join(lines)
