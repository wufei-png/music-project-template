from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from . import SCHEMA_VERSION
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


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    path: str
    message: str


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
    if record.get("record_type") in {"portfolio", "asset", "asset_location", "publication"}:
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
        if key == "record_id" or key in PATH_REFERENCE_FIELDS:
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


def _provider_profiles(root: Path) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for path in (root / "providers").glob("*/profile.toml"):
        profile = load_toml(path)
        profiles[str(profile.get("provider") or path.parent.name)] = profile
    return profiles


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
    records, load_issues = load_records(root)
    issues.extend(load_issues)
    registry: dict[str, tuple[Path, dict[str, Any]]] = {}
    profiles = _provider_profiles(root)

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

        if record.get("status") == "sealed" or record.get("seal"):
            seal = record.get("seal") or {}
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

        if record_type == "rights_evidence" and record.get("human_status") == "confirmed":
            evidence_hash = str(record.get("evidence_sha256") or "")
            if not record.get("private_locator") or not re.fullmatch(r"[0-9a-f]{64}", evidence_hash):
                issues.append(
                    Issue(
                        "error",
                        "rights_evidence",
                        relative,
                        "confirmed rights evidence requires an opaque locator and lowercase SHA-256",
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
            except (OSError, ValueError) as exc:
                issues.append(
                    Issue(
                        "error",
                        "terms_snapshot",
                        relative,
                        f"invalid terms snapshot {terms_snapshot_ref}: {exc}",
                    )
                )

        if record_type in {"generation", "edit"}:
            provider = str(record.get("provider") or "")
            profile = profiles.get(provider)
            if not profile:
                issues.append(Issue("error", "provider", relative, f"unknown provider profile: {provider}"))
            else:
                profile_version = str(profile.get("profile_version") or "")
                if record.get("provider_profile_version") != profile_version:
                    issues.append(Issue("error", "provider_version", relative, f"expected provider profile {profile_version}"))
                required_fields = ((profile.get("required") or {}).get(record_type) or {}).get("fields") or []
                for field in required_fields:
                    if record.get(field) in (None, "", []):
                        issues.append(Issue("error", "provider_required", relative, f"missing provider field: {field}"))
                operation_key = "generation_operations" if record_type == "generation" else "edit_operations"
                if record.get("provider_operation") not in (profile.get(operation_key) or []):
                    issues.append(Issue("error", "provider_operation", relative, f"unsupported operation: {record.get('provider_operation')}"))
                allowed_data = set(((profile.get("provider_data") or {}).get("allowed_keys") or []))
                unknown = set((record.get("provider_data") or {})) - allowed_data
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
                    issues.append(Issue("error", "asset_path", relative, str(exc)))
                    continue
                if actual != expected:
                    issues.append(Issue("error", "asset_hash", relative, "asset SHA-256 mismatch"))
                if storage == "git_lfs":
                    is_lfs, detail = _check_lfs_attribute(root, local_path)
                    if not is_lfs:
                        issues.append(Issue("error", "lfs_policy", relative, f"asset is not covered by Git LFS: {detail}"))
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
        if not record.get("frozen", {}).get("value"):
            issues.append(Issue("error", "release_not_frozen", relative, "release candidate is not frozen"))
        master = _resolve_ref(str(record.get("master_ref") or ""), record, registry)
        if not master or master[1].get("record_type") != "asset":
            issues.append(Issue("error", "master_ref", relative, "master_ref must resolve to an asset"))
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
                or (review[1].get("seal") or {}).get("content_sha256") != canonical_hash(review[1])
                or review[1].get("decision") not in {"approved", "selected"}
                or not review[1].get("human_confirmed")
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
        override = record.get("human_override") or {}
        override_valid = all(override.get(field) for field in ("reason", "actor", "timestamp", "unresolved_risks"))
        if not rights_refs and not override_valid:
            issues.append(Issue("error", "rights_gate", relative, "missing rights evidence and no valid override"))
        for reference in rights_refs:
            evidence = _resolve_ref(reference, record, registry)
            if (
                not evidence
                or evidence[1].get("record_type") != "rights_evidence"
                or evidence[1].get("status") != "sealed"
                or (evidence[1].get("seal") or {}).get("content_sha256") != canonical_hash(evidence[1])
                or not evidence[1].get("human_confirmed")
            ):
                issues.append(Issue("error", "rights_gate", relative, f"invalid rights evidence: {reference}"))
                continue
            human_status = evidence[1].get("human_status")
            if human_status == "known_unlicensed":
                issues.append(Issue("error", "known_unlicensed", relative, f"non-overridable rights blocker: {reference}"))
            elif human_status != "confirmed" and not override_valid:
                issues.append(Issue("error", "rights_gate", relative, f"unconfirmed rights evidence: {reference}"))
        frozen_files = list(record.get("frozen_files") or [])
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
            "metadata": str(record.get("metadata_path") or ""),
            "gate": str(record.get("gate_path") or ""),
            "checksums": str(record.get("checksums_path") or ""),
        }
        for kind, expected_path in required_frozen.items():
            paths = [str(item.get("path") or "") for item in frozen_by_kind.get(kind, [])]
            if paths != [expected_path]:
                issues.append(Issue("error", "frozen_file", relative, f"missing or inconsistent frozen {kind}"))
        policy_paths = [str(item.get("path") or "") for item in frozen_by_kind.get("policy_snapshot", [])]
        if policy_paths != list(record.get("policy_snapshot_refs") or []):
            issues.append(Issue("error", "policy_snapshot", relative, "frozen policy snapshots are inconsistent"))

        gate_path = str(record.get("gate_path") or "")
        try:
            gate = load_toml(resolve_inside(Path(gate_path), root, must_exist=True))
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            issues.append(Issue("error", "release_gate", relative, f"invalid frozen gate: {exc}"))
        else:
            expected_gate_values = {
                "listening_gate_refs": review_refs,
                "rights_refs": rights_refs,
                "policy_snapshot_refs": list(record.get("policy_snapshot_refs") or []),
            }
            for field, expected_value in expected_gate_values.items():
                if gate.get(field) != expected_value:
                    issues.append(Issue("error", "release_gate", relative, f"gate {field} does not match release candidate"))
            expected_status = "PASS_WITH_OVERRIDE" if record.get("human_override") else "PASS"
            if gate.get("status") != expected_status:
                issues.append(Issue("error", "release_gate", relative, "gate status does not match release candidate"))

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
        elif master:
            expected_digest = str(master[1].get("sha256") or "").removeprefix("sha256:")
            expected_name = Path(str(record.get("staging_path") or "")).name
            if checksum_entries.get(expected_name) != expected_digest:
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
