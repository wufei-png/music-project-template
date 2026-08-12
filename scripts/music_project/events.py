from __future__ import annotations

import csv
import json
import math
import re
import shutil
import stat
import uuid
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import SCHEMA_VERSION
from .analysis import analyze_audio
from .io import (
    atomic_write_text,
    atomic_write_toml,
    canonical_hash,
    load_toml,
    now_iso,
    parse_key_values,
    relative_path,
    resolve_inside,
    sha256_file,
    validate_identifier,
)
from .locking import LedgerBusyError, LedgerLock
from .records import (
    _next_id,
    _policy_snapshot,
    _provider_version,
    _track_directory,
    common_record,
    seal_is_valid,
    seal_record,
)
from .validation import load_records, validate

EVENT_TYPES = {
    "generation",
    "edit",
    "export",
    "render",
    "review",
    "rights_evidence",
    "release_candidate",
}
RESPONSIBLE_FIELDS = {
    "review": "reviewed_by",
    "rights_evidence": "assessed_by",
    "release_candidate": "confirmed_by",
}
ACTOR_TYPES = {"human", "agent", "automation"}
_EVENT_PREFIX = {
    "generation": ("generations", "g"),
    "edit": ("edits", "e"),
    "export": ("exports", "x"),
    "render": ("renders", "r"),
    "review": ("reviews", "lr"),
    "rights_evidence": ("rights", "re"),
}
_PAYLOAD_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    "generation": (
        {"provider", "operation", "model", "object_id", "prompt_ref"},
        {
            "occurred_at",
            "plan",
            "lyrics_ref",
            "terms_snapshot_ref",
            "parent_refs",
            "input_refs",
            "output_refs",
            "provider_data",
        },
    ),
    "edit": (
        {"provider", "operation", "parent_refs"},
        {"occurred_at", "input_refs", "output_refs", "provider_data"},
    ),
    "export": (
        {"provider", "operation", "parent_refs", "output_refs"},
        {"occurred_at", "input_refs", "provider_data"},
    ),
    "render": (
        {"toolchain", "parent_refs"},
        {"input_refs", "output_refs", "notes"},
    ),
    "review": (
        {"subject_ref", "decision"},
        {"subject_sha256", "blind_label", "timestamp_notes"},
    ),
    "rights_evidence": (
        {"subject_ref", "source_type", "human_status"},
        {"evidence_ref", "evidence_sha256", "public_note"},
    ),
    "release_candidate": (
        {"master", "title", "listening_gate_refs", "policy_snapshot_refs"},
        {"release_id", "rights_refs", "override_reason"},
    ),
}


class LedgerError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.details:
            error["details"] = self.details
        return {"error": error}


def require_supported_schema(root: Path) -> dict[str, Any]:
    try:
        config = load_toml(root / "music.toml")
    except (OSError, ValueError) as exc:
        raise LedgerError("invalid_ledger", f"cannot read music.toml: {exc}") from exc
    actual = config.get("schema_version")
    if actual != SCHEMA_VERSION:
        raise LedgerError(
            "unsupported_schema",
            "ledger schema is not supported; rebuild it with the current template",
            details={"expected": SCHEMA_VERSION, "actual": actual},
        )
    return config


def load_envelope(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError("invalid_envelope", f"cannot read event envelope: {exc}") from exc
    if not isinstance(value, dict):
        raise LedgerError("invalid_envelope", "event envelope must be a JSON object")
    return value


def load_plan_receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(
            "invalid_plan_receipt", f"cannot read release plan receipt: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise LedgerError("invalid_plan_receipt", "plan receipt must be a JSON object")
    return value


def _identity(value: Any, field: str, *, human_only: bool = False) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"type", "id"}:
        raise LedgerError(
            "invalid_envelope", f"{field} must contain exactly type and id"
        )
    actor_type = value.get("type")
    actor_id = value.get("id")
    allowed = {"human"} if human_only else ACTOR_TYPES
    if actor_type not in allowed or not isinstance(actor_id, str) or not actor_id.strip():
        expected = "human" if human_only else "human, agent, or automation"
        raise LedgerError(
            "invalid_envelope", f"{field} requires type {expected} and a non-empty id"
        )
    return {"type": str(actor_type), "id": actor_id.strip()}


def normalize_envelope(value: dict[str, Any]) -> dict[str, Any]:
    common = {"schema_version", "event_type", "submission_id", "submitted_by", "track_id", "payload"}
    if any(not isinstance(key, str) for key in value):
        raise LedgerError("invalid_envelope", "event envelope keys must be strings")
    event_type = value.get("event_type")
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise LedgerError("invalid_envelope", f"unsupported event_type: {event_type}")
    responsibility = RESPONSIBLE_FIELDS.get(str(event_type))
    allowed_top = common | ({responsibility} if responsibility else set())
    unknown_top = set(value) - allowed_top
    missing_top = common - set(value)
    if unknown_top or missing_top:
        raise LedgerError(
            "invalid_envelope",
            "event envelope fields do not match the contract",
            details={"missing": sorted(missing_top), "unknown": sorted(unknown_top)},
        )
    if value.get("schema_version") != SCHEMA_VERSION:
        raise LedgerError(
            "unsupported_schema",
            "event envelope schema is not supported",
            details={"expected": SCHEMA_VERSION, "actual": value.get("schema_version")},
        )
    submission_id = value.get("submission_id")
    try:
        canonical_submission_id = str(uuid.UUID(str(submission_id)))
    except (ValueError, AttributeError) as exc:
        raise LedgerError(
            "invalid_envelope", "submission_id must be a canonical UUID"
        ) from exc
    if str(submission_id) != canonical_submission_id:
        raise LedgerError("invalid_envelope", "submission_id must be a canonical UUID")
    track_id_value = value.get("track_id")
    if not isinstance(track_id_value, str):
        raise LedgerError("invalid_envelope", "track_id must be a string")
    track_id = track_id_value
    try:
        validate_identifier(track_id, "track-id")
    except ValueError as exc:
        raise LedgerError("invalid_envelope", str(exc)) from exc
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise LedgerError("invalid_envelope", "payload must be a JSON object")
    if any(not isinstance(key, str) for key in payload):
        raise LedgerError("invalid_envelope", "payload keys must be strings")
    required, optional = _PAYLOAD_FIELDS[str(event_type)]
    missing = required - set(payload)
    unknown = set(payload) - required - optional
    if missing or unknown:
        raise LedgerError(
            "invalid_envelope",
            f"{event_type} payload fields do not match the contract",
            details={"missing": sorted(missing), "unknown": sorted(unknown)},
        )
    try:
        normalized_payload = json.loads(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        )
    except (TypeError, ValueError) as exc:
        raise LedgerError(
            "invalid_envelope", "payload must contain finite JSON values"
        ) from exc
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "event_type": event_type,
        "submission_id": canonical_submission_id,
        "submitted_by": _identity(value.get("submitted_by"), "submitted_by"),
        "track_id": track_id,
        "payload": normalized_payload,
    }
    if responsibility:
        normalized[responsibility] = _identity(
            value.get(responsibility), responsibility, human_only=True
        )
    _validate_payload_shape(normalized)
    return normalized


def envelope_digest(envelope: dict[str, Any]) -> str:
    return canonical_hash(envelope)


def _string(payload: dict[str, Any], field: str, *, allow_empty: bool = False) -> str:
    value = payload.get(field, "")
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise LedgerError("invalid_envelope", f"payload.{field} must be a non-empty string")
    return value


def _string_list(payload: dict[str, Any], field: str, *, required: bool = False) -> list[str]:
    value = payload.get(field, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise LedgerError("invalid_envelope", f"payload.{field} must be a list of non-empty strings")
    if required and not value:
        raise LedgerError("invalid_envelope", f"payload.{field} must not be empty")
    return list(value)


def _validate_payload_shape(envelope: dict[str, Any]) -> None:
    event_type = str(envelope["event_type"])
    payload = envelope["payload"]
    if event_type in {"generation", "edit", "export"}:
        for field in ("provider", "operation"):
            _string(payload, field)
        provider_data = payload.get("provider_data", {})
        if not isinstance(provider_data, dict):
            raise LedgerError("invalid_envelope", "payload.provider_data must be an object")
        if any(
            not isinstance(value, (str, int, float, bool))
            or (isinstance(value, float) and not math.isfinite(value))
            for value in provider_data.values()
        ):
            raise LedgerError(
                "invalid_envelope",
                "payload.provider_data values must be finite string, number, or boolean scalars",
            )
        for field in ("occurred_at",):
            if field in payload:
                _string(payload, field, allow_empty=True)
        for field in ("parent_refs", "input_refs", "output_refs"):
            _string_list(payload, field)
    if event_type == "generation":
        for field in ("model", "object_id", "prompt_ref"):
            _string(payload, field)
        for field in ("plan", "lyrics_ref", "terms_snapshot_ref"):
            if field in payload:
                _string(payload, field, allow_empty=True)
    if event_type in {"edit", "export", "render"}:
        _string_list(payload, "parent_refs", required=True)
    if event_type == "export":
        _string_list(payload, "output_refs", required=True)
    if event_type == "render":
        _string(payload, "toolchain")
        _string_list(payload, "input_refs")
        _string_list(payload, "output_refs")
        if "notes" in payload:
            _string(payload, "notes", allow_empty=True)
    if event_type == "review":
        _string(payload, "subject_ref")
        decision = _string(payload, "decision")
        allowed = {"relisten", "shortlist", "selected", "needs_work", "rejected", "approved"}
        if decision not in allowed:
            raise LedgerError("invalid_envelope", f"unsupported review decision: {decision}")
        digest = payload.get("subject_sha256", "")
        if "subject_sha256" in payload:
            _string(payload, "subject_sha256", allow_empty=True)
        if digest and not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", digest):
            raise LedgerError("invalid_envelope", "payload.subject_sha256 is malformed")
        for field in ("blind_label", "timestamp_notes"):
            if field in payload:
                _string(payload, field, allow_empty=True)
    if event_type == "rights_evidence":
        for field in ("subject_ref", "source_type", "human_status"):
            _string(payload, field)
        status = payload["human_status"]
        allowed = {"confirmed", "needs_review", "missing", "known_unlicensed"}
        if status not in allowed:
            raise LedgerError("invalid_envelope", f"unsupported rights status: {status}")
        for field in ("evidence_ref", "evidence_sha256", "public_note"):
            if field in payload:
                _string(payload, field, allow_empty=True)
        evidence_ref = payload.get("evidence_ref", "")
        evidence_hash = payload.get("evidence_sha256", "")
        if status == "confirmed" and (
            not evidence_ref or not re.fullmatch(r"[0-9a-f]{64}", evidence_hash)
        ):
            raise LedgerError(
                "invalid_envelope",
                "confirmed rights evidence requires evidence_ref and lowercase evidence_sha256",
            )
        if evidence_hash and not re.fullmatch(r"[0-9a-f]{64}", evidence_hash):
            raise LedgerError(
                "invalid_envelope", "payload.evidence_sha256 is malformed"
            )
        if evidence_ref:
            _validate_safe_evidence_ref(evidence_ref)
    if event_type == "release_candidate":
        for field in ("master", "title"):
            _string(payload, field)
        _string_list(payload, "listening_gate_refs", required=True)
        _string_list(payload, "policy_snapshot_refs", required=True)
        _string_list(payload, "rights_refs")
        for field in ("release_id", "override_reason"):
            if field in payload:
                _string(payload, field, allow_empty=True)
        if payload.get("release_id") and not re.fullmatch(
            r"rc[0-9]{3,}", payload["release_id"]
        ):
            raise LedgerError("invalid_envelope", "payload.release_id must use rcNNN form")


def _validate_safe_evidence_ref(reference: str) -> None:
    if Path(reference).is_absolute() or reference.startswith(("~/", "~\\")):
        raise LedgerError("unsafe_evidence_ref", "evidence_ref must not be an absolute path")
    parsed = urlparse(reference)
    if parsed.scheme.lower() == "file":
        raise LedgerError(
            "unsafe_evidence_ref", "evidence_ref must not expose a local file locator"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LedgerError(
            "unsafe_evidence_ref", "evidence_ref must not contain credentials, query, or fragment"
        )
    if re.search(r"(?i)(?:token|secret|password|api[_-]?key)=", reference):
        raise LedgerError("unsafe_evidence_ref", "evidence_ref appears to contain a secret")


def _baseline_validation(root: Path) -> None:
    report = validate(root, "minimal")
    if report["status"] != "PASS":
        codes = sorted({item["code"] for item in report["errors"]})
        raise LedgerError(
            "ledger_invalid",
            "ledger failed minimal validation: " + ", ".join(codes),
            details={"codes": codes},
        )


def _all_records(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    records, issues = load_records(root)
    if any(issue.severity == "error" for issue in issues):
        raise LedgerError("ledger_invalid", "canonical TOML records do not parse")
    return records


def _submission_result(
    root: Path, submission_id: str, digest: str
) -> dict[str, Any] | None:
    matches = [
        (path, record)
        for path, record in _all_records(root)
        if record.get("submission_id") == submission_id
    ]
    if len(matches) > 1:
        raise LedgerError("ledger_invalid", "submission_id appears in multiple sealed records")
    if not matches:
        return None
    path, record = matches[0]
    if record.get("status") != "sealed" or not seal_is_valid(record):
        raise LedgerError(
            "ledger_invalid", "submission_id is bound to an invalid sealed record"
        )
    if record.get("envelope_digest") != digest:
        raise LedgerError(
            "submission_conflict",
            "submission_id was already applied with a different envelope digest",
        )
    return {
        "status": "idempotent",
        "record_path": relative_path(path, root),
        "record": record,
    }


def _record_lookup(
    root: Path, track_ref: str, reference: str, allowed_types: set[str]
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for _, record in _all_records(root):
        if record.get("record_id") != reference:
            continue
        scope = str(record.get("track_ref") or "global")
        if scope in {track_ref, "global"}:
            matches.append(record)
    if len(matches) != 1 or matches[0].get("record_type") not in allowed_types:
        raise LedgerError("invalid_reference", f"reference is missing, ambiguous, or wrong type: {reference}")
    record = matches[0]
    if record.get("status") == "sealed" and not seal_is_valid(record):
        raise LedgerError("invalid_reference", f"reference has an invalid seal: {reference}")
    return record


def _validate_refs(root: Path, envelope: dict[str, Any]) -> None:
    event_type = str(envelope["event_type"])
    payload = envelope["payload"]
    track_ref = f"track:{envelope['track_id']}"
    event_refs = {"generation", "edit", "export", "render"}
    for reference in payload.get("parent_refs", []):
        _record_lookup(root, track_ref, reference, event_refs)
    for field in ("input_refs", "output_refs"):
        for reference in payload.get(field, []):
            _record_lookup(root, track_ref, reference, {"asset"})
    if event_type == "generation":
        _record_lookup(root, track_ref, payload["prompt_ref"], {"prompt"})
        if payload.get("lyrics_ref"):
            _record_lookup(root, track_ref, payload["lyrics_ref"], {"lyrics"})
    if event_type in {"review", "rights_evidence"}:
        _record_lookup(
            root,
            track_ref,
            payload["subject_ref"],
            {"track", *event_refs, "asset", "release_candidate"},
        )


def _validate_provider(root: Path, event_type: str, payload: dict[str, Any]) -> None:
    if event_type not in {"generation", "edit", "export"}:
        return
    provider = str(payload["provider"])
    profile_path = root / "providers" / provider / "profile.toml"
    if not profile_path.is_file():
        raise LedgerError("invalid_provider", f"unknown provider profile: {provider}")
    profile = load_toml(profile_path)
    operation_field = {
        "generation": "generation_operations",
        "edit": "edit_operations",
        "export": "export_operations",
    }[event_type]
    if payload["operation"] not in (profile.get(operation_field) or []):
        raise LedgerError("invalid_provider_operation", f"unsupported provider operation: {payload['operation']}")
    allowed_data = set((profile.get("provider_data") or {}).get("allowed_keys") or [])
    unknown_data = set(payload.get("provider_data") or {}) - allowed_data
    if unknown_data:
        raise LedgerError(
            "invalid_provider_data",
            "unknown provider_data keys",
            details={"unknown": sorted(unknown_data)},
        )


def _ordinary_record(root: Path, envelope: dict[str, Any], digest: str) -> tuple[Path, dict[str, Any]]:
    event_type = str(envelope["event_type"])
    track_id = str(envelope["track_id"])
    track = _track_directory(root, track_id)
    payload = envelope["payload"]
    _validate_refs(root, envelope)
    _validate_provider(root, event_type, payload)
    directory_name, prefix = _EVENT_PREFIX[event_type]
    record_id = _next_id(track / directory_name, prefix, event_type)
    path = track / directory_name / f"{record_id}.toml"
    record = common_record(
        event_type, record_id, submitted_by=envelope["submitted_by"]
    )
    record.update(
        {
            "submission_id": envelope["submission_id"],
            "envelope_digest": digest,
            "track_ref": f"track:{track_id}",
            "output_binding": {"record_path": relative_path(path, root)},
        }
    )
    if event_type in {"generation", "edit", "export"}:
        provider = str(payload["provider"])
        record.update(
            {
                "occurred_at": payload.get("occurred_at") or now_iso(),
                "provider": provider,
                "provider_profile_version": _provider_version(root, provider),
                "provider_operation": payload["operation"],
                "parent_refs": list(payload.get("parent_refs") or []),
                "input_refs": list(payload.get("input_refs") or []),
                "output_refs": list(payload.get("output_refs") or []),
                "provider_data": dict(payload.get("provider_data") or {}),
            }
        )
    if event_type == "generation":
        snapshot_ref, snapshot_hash = _policy_snapshot(
            root, str(payload["provider"]), str(payload.get("terms_snapshot_ref") or "")
        )
        record.update(
            {
                "model": payload["model"],
                "provider_object_id": payload["object_id"],
                "plan_at_operation": str(payload.get("plan") or ""),
                "terms_snapshot_ref": snapshot_ref,
                "terms_snapshot_sha256": snapshot_hash,
                "prompt_ref": payload["prompt_ref"],
                "lyrics_ref": str(payload.get("lyrics_ref") or ""),
            }
        )
    elif event_type == "render":
        record.update(
            {
                "toolchain": payload["toolchain"],
                "parent_refs": list(payload["parent_refs"]),
                "input_refs": list(payload.get("input_refs") or []),
                "output_refs": list(payload.get("output_refs") or []),
                "notes": str(payload.get("notes") or ""),
            }
        )
    elif event_type == "review":
        record.update(
            {
                "subject_ref": payload["subject_ref"],
                "subject_sha256": str(payload.get("subject_sha256") or "").removeprefix("sha256:"),
                "blind_label": str(payload.get("blind_label") or ""),
                "decision": payload["decision"],
                "timestamp_notes": str(payload.get("timestamp_notes") or ""),
                "reviewed_by": envelope["reviewed_by"],
            }
        )
    elif event_type == "rights_evidence":
        record.update(
            {
                "subject_ref": payload["subject_ref"],
                "source_type": payload["source_type"],
                "human_status": payload["human_status"],
                "evidence_ref": str(payload.get("evidence_ref") or ""),
                "evidence_sha256": str(payload.get("evidence_sha256") or ""),
                "public_note": str(payload.get("public_note") or ""),
                "assessed_by": envelope["assessed_by"],
            }
        )
    return path, seal_record(record)


def _rebuild_listening_view(root: Path, track_id: str) -> None:
    rows: list[list[str]] = []
    for path, record in _all_records(root):
        if record.get("record_type") != "review" or record.get("track_ref") != f"track:{track_id}":
            continue
        rows.append(
            [
                path.stem,
                str(record.get("subject_ref") or ""),
                str(record.get("subject_sha256") or ""),
                str((record.get("reviewed_by") or {}).get("id") or ""),
                str(record.get("blind_label") or ""),
                str(record.get("decision") or ""),
                str(record.get("timestamp_notes") or ""),
                str(record.get("created_at") or ""),
            ]
        )
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        ["review_id", "subject_ref", "subject_sha256", "reviewer", "blind_label", "decision", "timestamp_notes", "created_at"]
    )
    writer.writerows(sorted(rows))
    atomic_write_text(root / "tracks" / track_id / "reviews" / "listening.csv", output.getvalue())


def _next_release_id(root: Path) -> str:
    maximum = 0
    for path in (root / "releases").glob("rc[0-9]*"):
        match = re.fullmatch(r"rc(\d+)", path.name)
        if match:
            maximum = max(maximum, int(match.group(1)))
    return f"rc{maximum + 1:03d}"


def _policy_snapshots(root: Path, references: list[str]) -> list[dict[str, str]]:
    snapshots: list[dict[str, str]] = []
    for reference in references:
        try:
            path = resolve_inside(Path(reference), root, must_exist=True)
            relative = path.relative_to(root.resolve()).parts
            data = load_toml(path)
        except (OSError, ValueError) as exc:
            raise LedgerError("invalid_policy_snapshot", f"invalid policy snapshot: {reference}") from exc
        provider = str(data.get("provider") or "")
        if len(relative) < 4 or relative[:2] != ("policies", "platforms") or relative[2] != provider:
            raise LedgerError("invalid_policy_snapshot", f"policy snapshot identity mismatch: {reference}")
        snapshots.append(
            {
                "path": relative_path(path, root),
                "sha256": sha256_file(path),
                "provider": provider,
            }
        )
    return snapshots


def _release_plan_core(root: Path, envelope: dict[str, Any], digest: str) -> dict[str, Any]:
    report = validate(root, "post")
    if report["status"] != "PASS":
        codes = sorted({item["code"] for item in report["errors"]})
        raise LedgerError(
            "ledger_invalid",
            "ledger failed post validation before release planning: " + ", ".join(codes),
            details={"codes": codes},
        )
    track_id = str(envelope["track_id"])
    _track_directory(root, track_id)
    payload = envelope["payload"]
    master_raw = Path(str(payload["master"])).expanduser()
    master = (root / master_raw).resolve(strict=True) if not master_raw.is_absolute() else master_raw.resolve(strict=True)
    if not master.is_file():
        raise LedgerError("invalid_master", "release master must be a file")
    master_digest = sha256_file(master)
    track_ref = f"track:{track_id}"

    reviews: list[dict[str, str]] = []
    for reference in payload["listening_gate_refs"]:
        review = _record_lookup(root, track_ref, reference, {"review"})
        reviewed_by = review.get("reviewed_by") or {}
        if (
            review.get("decision") not in {"approved", "selected"}
            or reviewed_by.get("type") != "human"
            or str(review.get("subject_sha256") or "") != master_digest
        ):
            raise LedgerError("invalid_listening_gate", f"review is not bound to the exact master: {reference}")
        reviews.append({"record_id": reference, "seal": str((review.get("seal") or {}).get("content_sha256") or "")})

    track_rights = [
        record
        for _, record in _all_records(root)
        if record.get("record_type") == "rights_evidence" and record.get("track_ref") == track_ref
    ]
    blockers = [str(record.get("record_id") or "") for record in track_rights if record.get("human_status") == "known_unlicensed"]
    if blockers:
        raise LedgerError(
            "known_unlicensed",
            "known_unlicensed rights evidence blocks release candidate creation",
            details={"records": sorted(blockers)},
        )
    rights: list[dict[str, str]] = []
    statuses: list[str] = []
    for reference in payload.get("rights_refs", []):
        evidence = _record_lookup(root, track_ref, reference, {"rights_evidence"})
        assessed_by = evidence.get("assessed_by") or {}
        if assessed_by.get("type") != "human":
            raise LedgerError("invalid_rights_evidence", f"rights evidence lacks a human assessor: {reference}")
        status = str(evidence.get("human_status") or "")
        statuses.append(status)
        rights.append(
            {
                "record_id": reference,
                "status": status,
                "seal": str((evidence.get("seal") or {}).get("content_sha256") or ""),
            }
        )
    needs_override = not statuses or any(status != "confirmed" for status in statuses)
    override_reason = str(payload.get("override_reason") or "").strip()
    if needs_override and not override_reason:
        raise LedgerError(
            "rights_override_required",
            "unconfirmed or missing rights evidence requires override_reason",
        )
    gate_status = "PASS_WITH_OVERRIDE" if needs_override else "PASS"

    snapshots = _policy_snapshots(root, list(payload["policy_snapshot_refs"]))
    records = [
        record
        for _, record in _all_records(root)
        if record.get("track_ref") == track_ref
        and record.get("record_type") in {"generation", "edit", "export"}
    ]
    providers = {str(record.get("provider") or "") for record in records if record.get("provider")}
    frozen_providers = {item["provider"] for item in snapshots}
    if providers - frozen_providers:
        raise LedgerError(
            "missing_policy_snapshot",
            "release plan is missing a provider policy snapshot",
            details={"providers": sorted(providers - frozen_providers)},
        )
    snapshot_identities = {(item["path"], item["sha256"]) for item in snapshots}
    generation_snapshots = {
        (str(record.get("terms_snapshot_ref") or ""), str(record.get("terms_snapshot_sha256") or ""))
        for record in records
        if record.get("record_type") == "generation" and record.get("terms_snapshot_ref")
    }
    if generation_snapshots - snapshot_identities:
        raise LedgerError("missing_policy_snapshot", "release plan does not bind the exact generation policy snapshot")

    release_id = str(payload.get("release_id") or _next_release_id(root))
    release_dir = root / "releases" / release_id
    if release_dir.exists():
        raise LedgerError("release_exists", f"release candidate already exists: {release_id}")
    track_records = [
        record
        for _, record in _all_records(root)
        if record.get("track_ref") == track_ref
    ]
    state_records = [
        record
        for record in track_records
        if record.get("record_type")
        in {"generation", "edit", "export", "render", "review", "rights_evidence"}
    ]
    text_refs = {
        str(record.get(field) or "")
        for record in state_records
        if record.get("record_type") == "generation"
        for field in ("prompt_ref", "lyrics_ref")
        if record.get(field)
    }
    by_id = {
        str(record.get("record_id") or ""): record
        for record in track_records
    }
    for reference in sorted(text_refs):
        prerequisite = by_id.get(reference)
        if prerequisite is None or prerequisite.get("record_type") not in {
            "prompt",
            "lyrics",
        }:
            raise LedgerError(
                "invalid_reference",
                f"release prerequisite is missing or wrong type: {reference}",
            )
        state_records.append(prerequisite)
    state_items = []
    for record in state_records:
        item = {
            "record_type": str(record.get("record_type") or ""),
            "record_id": str(record.get("record_id") or ""),
            "seal": str((record.get("seal") or {}).get("content_sha256") or ""),
        }
        if record.get("record_type") in {"prompt", "lyrics"}:
            item["text_sha256"] = str(record.get("sha256") or "")
        state_items.append(item)
    state_items.sort(key=lambda item: (item["record_type"], item["record_id"]))
    core = {
        "envelope": envelope,
        "envelope_digest": digest,
        "resolved_master": {
            "path": str(master),
            "sha256": master_digest,
            "size_bytes": master.stat().st_size,
            "name": master.name,
        },
        "resolved_reviews": reviews,
        "resolved_rights": rights,
        "policy_snapshots": snapshots,
        "expected_output": {
            "release_id": release_id,
            "release_directory": relative_path(release_dir, root),
            "record_path": relative_path(release_dir / "rc.toml", root),
            "master_path": relative_path(release_dir / "master" / master.name, root),
        },
        "gate_status": gate_status,
        "override_reason": override_reason,
        "ledger_state": state_items,
    }
    core["ledger_state_digest"] = canonical_hash({"items": state_items})
    return core


def _release_plan_receipt(root: Path, envelope: dict[str, Any], digest: str) -> dict[str, Any]:
    core = _release_plan_core(root, envelope, digest)
    return {
        "receipt_type": "release_plan",
        "schema_version": SCHEMA_VERSION,
        "submission_id": envelope["submission_id"],
        "envelope_digest": digest,
        "plan_digest": canonical_hash(core),
        "planned_at": now_iso(),
        "plan": core,
    }


def _reject_staging_symlink(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise LedgerError(
            "unsafe_staging", f"release staging path must not be a symlink: {path}"
        )


def _prepare_release_staging(root: Path, submission_id: str) -> tuple[Path, Path]:
    releases = root.resolve() / "releases"
    staging_root = releases / ".staging"
    for component in (releases, staging_root):
        _reject_staging_symlink(component)
        if component.exists() and not component.is_dir():
            raise LedgerError(
                "unsafe_staging", f"release staging component is not a directory: {component}"
            )
    staging_root.mkdir(parents=True, exist_ok=True)
    _reject_staging_symlink(staging_root)

    staging = staging_root / submission_id
    marker = staging / ".submission-id"
    _reject_staging_symlink(staging)
    if staging.exists():
        if not staging.is_dir():
            raise LedgerError(
                "staging_conflict", "release submission staging is not a directory"
            )
        _reject_staging_symlink(marker)
        try:
            owner = marker.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise LedgerError(
                "staging_conflict",
                "release submission staging has no valid ownership marker",
            ) from exc
        if owner != submission_id:
            raise LedgerError(
                "staging_conflict",
                "release submission staging belongs to a different submission",
            )
        shutil.rmtree(staging)

    staging.mkdir()
    try:
        atomic_write_text(marker, submission_id + "\n")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging, marker


def plan_event(root: Path, raw_envelope: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
    try:
        with LedgerLock(root, timeout):
            require_supported_schema(root)
            envelope = normalize_envelope(raw_envelope)
            digest = envelope_digest(envelope)
            existing = _submission_result(root, envelope["submission_id"], digest)
            if existing:
                return existing
            _baseline_validation(root)
            if envelope["event_type"] == "release_candidate":
                return _release_plan_receipt(root, envelope, digest)
            _ordinary_record(root, envelope, digest)
            directory, prefix = _EVENT_PREFIX[str(envelope["event_type"])]
            track = _track_directory(root, str(envelope["track_id"]))
            preview_id = _next_id(track / directory, prefix, str(envelope["event_type"]))
            return {
                "plan_type": "ordinary_event",
                "schema_version": SCHEMA_VERSION,
                "submission_id": envelope["submission_id"],
                "envelope_digest": digest,
                "event_type": envelope["event_type"],
                "binding_required": False,
                "preview_record_id": f"{envelope['event_type']}:{preview_id}",
                "note": "apply revalidates and may allocate a different record ID",
            }
    except LedgerBusyError as exc:
        raise LedgerError("ledger_busy", str(exc)) from exc


def _apply_release(
    root: Path,
    envelope: dict[str, Any],
    digest: str,
    plan_receipt: dict[str, Any] | None,
) -> dict[str, Any]:
    if not plan_receipt:
        raise LedgerError("plan_receipt_required", "release_candidate requires a Release Plan Receipt")
    provided_plan = plan_receipt.get("plan")
    if not isinstance(provided_plan, dict) or canonical_hash(provided_plan) != plan_receipt.get(
        "plan_digest"
    ):
        raise LedgerError(
            "invalid_plan_receipt", "release plan receipt content does not match its digest"
        )
    recomputed = _release_plan_receipt(root, envelope, digest)
    identity_fields = ("receipt_type", "schema_version", "submission_id", "envelope_digest", "plan_digest")
    if any(plan_receipt.get(field) != recomputed.get(field) for field in identity_fields):
        raise LedgerError(
            "plan_stale",
            "release plan no longer matches the envelope or ledger state; plan again and reconfirm",
        )
    plan = recomputed["plan"]
    output = plan["expected_output"]
    release_id = output["release_id"]
    release_dir = root / output["release_directory"]
    staging, staging_marker = _prepare_release_staging(
        root, str(envelope["submission_id"])
    )
    bundle = staging / release_id
    bundle.mkdir(parents=True)
    try:
        master_source = Path(plan["resolved_master"]["path"])
        master_path = bundle / "master" / plan["resolved_master"]["name"]
        master_path.parent.mkdir()
        shutil.copy2(master_source, master_path)
        if sha256_file(master_path) != plan["resolved_master"]["sha256"]:
            raise LedgerError("master_changed", "release master changed during staging")
        analysis_path = bundle / "analysis.json"
        analyze_audio(root, master_path, analysis_path)
        metadata_path = bundle / "metadata.toml"
        atomic_write_toml(
            metadata_path,
            {
                "schema_version": SCHEMA_VERSION,
                "release_id": release_id,
                "title": envelope["payload"]["title"],
                "release_type": "single",
                "track_refs": [f"track:{envelope['track_id']}"],
                "content_license": "project_defined",
                "publication_target": "github_release",
                "github_tag": f"release/{release_id}",
            },
        )
        gate_path = bundle / "gate.toml"
        gate: dict[str, Any] = {
            "status": plan["gate_status"],
            "checked_at": now_iso(),
            "confirmed_by": envelope["confirmed_by"],
            "listening_gate_refs": list(envelope["payload"]["listening_gate_refs"]),
            "rights_refs": list(envelope["payload"].get("rights_refs") or []),
            "policy_snapshot_refs": [item["path"] for item in plan["policy_snapshots"]],
            "note": "PASS_WITH_OVERRIDE is not rights-cleared; publication is separate.",
        }
        if plan["gate_status"] == "PASS_WITH_OVERRIDE":
            gate["override_reason"] = plan["override_reason"]
        atomic_write_toml(gate_path, gate)
        checksums_path = bundle / "checksums.sha256"
        atomic_write_text(
            checksums_path,
            f"{plan['resolved_master']['sha256']}  master/{plan['resolved_master']['name']}\n",
        )
        relative_bundle = Path("releases") / release_id
        files = []
        for kind, staged_path, final_relative in (
            ("master", master_path, relative_bundle / "master" / master_path.name),
            ("metadata", metadata_path, relative_bundle / "metadata.toml"),
            ("gate", gate_path, relative_bundle / "gate.toml"),
            ("analysis", analysis_path, relative_bundle / "analysis.json"),
            ("checksums", checksums_path, relative_bundle / "checksums.sha256"),
        ):
            files.append({"kind": kind, "path": final_relative.as_posix(), "sha256": sha256_file(staged_path)})
        record = common_record(
            "release_candidate", release_id, submitted_by=envelope["submitted_by"]
        )
        record.update(
            {
                "submission_id": envelope["submission_id"],
                "envelope_digest": digest,
                "plan_digest": recomputed["plan_digest"],
                "track_ref": f"track:{envelope['track_id']}",
                "confirmed_by": envelope["confirmed_by"],
                "gate_status": plan["gate_status"],
                "override_reason": plan["override_reason"] if plan["gate_status"] == "PASS_WITH_OVERRIDE" else "",
                "master_sha256": plan["resolved_master"]["sha256"],
                "master_size_bytes": plan["resolved_master"]["size_bytes"],
                "master_path": output["master_path"],
                "metadata_path": f"releases/{release_id}/metadata.toml",
                "gate_path": f"releases/{release_id}/gate.toml",
                "analysis_path": f"releases/{release_id}/analysis.json",
                "checksums_path": f"releases/{release_id}/checksums.sha256",
                "listening_gate_refs": list(envelope["payload"]["listening_gate_refs"]),
                "rights_refs": list(envelope["payload"].get("rights_refs") or []),
                "policy_snapshot_refs": [item["path"] for item in plan["policy_snapshots"]],
                "policy_snapshots": plan["policy_snapshots"],
                "output_binding": {
                    "record_path": output["record_path"],
                    "release_directory": output["release_directory"],
                    "files": files,
                },
                "publication_status": "prepared",
                "frozen": {"value": True, "by": envelope["confirmed_by"]["id"], "at": now_iso()},
            }
        )
        atomic_write_toml(bundle / "rc.toml", seal_record(record))
        bundle.replace(release_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # The bundle rename above is the commit point. Cleanup must not turn an
    # already committed release into an apparent failure.
    try:
        staging_marker.unlink()
        staging.rmdir()
    except OSError:
        pass
    final_path = release_dir / "rc.toml"
    return {
        "status": "applied",
        "record_path": relative_path(final_path, root),
        "record": load_toml(final_path),
    }


def apply_event(
    root: Path,
    raw_envelope: dict[str, Any],
    *,
    confirmed: bool,
    plan_receipt: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    if not confirmed:
        raise LedgerError("confirmation_required", "apply-event requires --confirm")
    try:
        with LedgerLock(root, timeout):
            require_supported_schema(root)
            envelope = normalize_envelope(raw_envelope)
            digest = envelope_digest(envelope)
            existing = _submission_result(root, envelope["submission_id"], digest)
            if existing:
                return existing
            _baseline_validation(root)
            if envelope["event_type"] == "release_candidate":
                return _apply_release(root, envelope, digest, plan_receipt)
            path, record = _ordinary_record(root, envelope, digest)
            atomic_write_toml(path, record)
            derived_status = "not_applicable"
            if envelope["event_type"] == "review":
                try:
                    _rebuild_listening_view(root, str(envelope["track_id"]))
                    derived_status = "current"
                except OSError:
                    derived_status = "stale"
            return {
                "status": "applied",
                "record_path": relative_path(path, root),
                "record": record,
                "derived_view_status": derived_status,
            }
    except LedgerBusyError as exc:
        raise LedgerError("ledger_busy", str(exc)) from exc


def payload_from_key_values(values: list[str]) -> dict[str, Any]:
    """CLI helper retaining KEY=VALUE convenience without changing envelopes."""
    return parse_key_values(values)
