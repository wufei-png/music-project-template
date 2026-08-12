from __future__ import annotations

import tempfile
import unittest
import zipfile
import os
import json
import subprocess
import uuid
import wave
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch
from pathlib import Path

from scripts.music_project.assets import create_asset_record, safe_import
from scripts.music_project.cli import _hash_text, initialize_project, main as cli_main
from scripts.music_project.events import LedgerError, apply_event, plan_event
from scripts.music_project.io import atomic_write_toml, load_toml, parse_key_values, sha256_file
from scripts.music_project.locking import LedgerLock
from scripts.music_project.records import _next_id, new_track, seal_record
from scripts.music_project.release import record_publication
from scripts.music_project.retention import apply_plan, build_plan, effective_policy
from scripts.music_project.validation import validate


ACTOR = "fixture-user"


def _apply_test_event(
    root: Path,
    event_type: str,
    track_id: str,
    payload: dict[str, object],
    actor: str,
    responsibility: str = "",
) -> Path:
    envelope: dict[str, object] = {
        "schema_version": "1.0",
        "event_type": event_type,
        "submission_id": str(uuid.uuid4()),
        "submitted_by": {"type": "human", "id": actor},
        "track_id": track_id,
        "payload": payload,
    }
    if responsibility:
        envelope[f"{responsibility}_by"] = {"type": "human", "id": actor}
    result = apply_event(root, envelope, confirmed=True)
    return root / str(result["record_path"])


def register_generation(root: Path, **values: object) -> Path:
    actor = str(values.pop("actor"))
    track_id = str(values.pop("track_id"))
    payload = {
        "provider": values.pop("provider"),
        "operation": values.pop("operation"),
        "occurred_at": values.pop("occurred_at"),
        "model": values.pop("model"),
        "object_id": values.pop("object_id"),
        "plan": values.pop("plan"),
        "prompt_ref": values.pop("prompt_ref"),
        "lyrics_ref": values.pop("lyrics_ref"),
        "terms_snapshot_ref": values.pop("terms_snapshot_ref"),
        "parent_refs": values.pop("parent_refs"),
        "input_refs": values.pop("input_refs"),
        "output_refs": values.pop("output_refs"),
        "provider_data": parse_key_values(values.pop("provider_data")),
    }
    assert not values
    return _apply_test_event(root, "generation", track_id, payload, actor)


def register_export(root: Path, **values: object) -> Path:
    actor = str(values.pop("actor"))
    track_id = str(values.pop("track_id"))
    payload = {
        "provider": values.pop("provider"),
        "operation": values.pop("operation"),
        "occurred_at": values.pop("occurred_at"),
        "parent_refs": values.pop("parent_refs"),
        "input_refs": values.pop("input_refs"),
        "output_refs": values.pop("output_refs"),
        "provider_data": parse_key_values(values.pop("provider_data")),
    }
    assert not values
    return _apply_test_event(root, "export", track_id, payload, actor)


def record_review(root: Path, **values: object) -> Path:
    actor = str(values.pop("actor"))
    track_id = str(values.pop("track_id"))
    payload = {
        "subject_ref": values.pop("subject_ref"),
        "subject_sha256": values.pop("subject_sha256", ""),
        "decision": values.pop("decision"),
        "blind_label": values.pop("blind_label"),
        "timestamp_notes": values.pop("timestamp_notes"),
    }
    assert not values
    return _apply_test_event(root, "review", track_id, payload, actor, "reviewed")


def record_rights(root: Path, **values: object) -> Path:
    actor = str(values.pop("actor"))
    track_id = str(values.pop("track_id"))
    payload = {
        "subject_ref": values.pop("subject_ref"),
        "source_type": values.pop("source_type"),
        "human_status": values.pop("human_status"),
        "evidence_ref": values.pop("evidence_ref", values.pop("private_locator", "")),
        "evidence_sha256": values.pop("evidence_sha256"),
        "public_note": values.pop("public_note"),
    }
    assert not values
    return _apply_test_event(root, "rights_evidence", track_id, payload, actor, "assessed")


def freeze_release(root: Path, **values: object) -> tuple[Path, str]:
    actor = str(values.pop("actor"))
    track_id = str(values.pop("track_id"))
    confirmed = bool(values.pop("confirmed"))
    values.pop("override_risks", [])
    payload = {
        "master": str(values.pop("master")),
        "title": values.pop("title"),
        "release_id": values.pop("release_id"),
        "listening_gate_refs": values.pop("listening_gate_refs"),
        "rights_refs": values.pop("rights_refs"),
        "policy_snapshot_refs": values.pop("policy_snapshot_refs"),
        "override_reason": values.pop("override_reason"),
    }
    assert not values
    envelope = {
        "schema_version": "1.0",
        "event_type": "release_candidate",
        "submission_id": str(uuid.uuid4()),
        "submitted_by": {"type": "human", "id": actor},
        "confirmed_by": {"type": "human", "id": actor},
        "track_id": track_id,
        "payload": payload,
    }
    receipt = plan_event(root, envelope)
    result = apply_event(root, envelope, confirmed=confirmed, plan_receipt=receipt)
    return root / str(result["record_path"]), str(result["record"]["gate_status"])


class ProjectCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="music-template-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "project"
        initialize_project(self.root, "fixture", "Fixture Project", ACTOR)

    def new_track(self, track_id: str = "track-one") -> None:
        new_track(self.root, track_id, "Track One", ACTOR)

    def seal_texts(self, track_id: str = "track-one") -> None:
        _hash_text(self.root, track_id, "prompt", "p001")
        _hash_text(self.root, track_id, "lyrics", "l001")

    def approved_review(
        self, track_id: str = "track-one", subject_sha256: str = ""
    ) -> str:
        path = record_review(
            self.root,
            track_id=track_id,
            actor=ACTOR,
            subject_ref=f"track:{track_id}",
            decision="approved",
            blind_label="A",
            timestamp_notes="full listen complete",
            subject_sha256=subject_sha256,
        )
        return f"review:{path.stem}"

    def rights(self, status: str, track_id: str = "track-one") -> str:
        path = record_rights(
            self.root,
            track_id=track_id,
            actor=ACTOR,
            subject_ref=f"track:{track_id}",
            source_type="original",
            human_status=status,
            private_locator="private-ledger:item-1",
            evidence_sha256="0" * 64,
            public_note="fixture evidence",
        )
        return f"rights_evidence:{path.stem}"


class InitializationTests(ProjectCase):
    def test_empty_project_and_scoped_ids_validate(self) -> None:
        covered_report = validate(self.root, "minimal")
        self.assertEqual(covered_report["status"], "PASS", covered_report)
        self.new_track("first")
        self.new_track("second")
        report = validate(self.root, "minimal")
        self.assertEqual(report["status"], "PASS", report)
        self.assertEqual(load_toml(self.root / "music.toml")["project"]["project_id"], "fixture")

    def test_initializer_preserves_unrelated_existing_file(self) -> None:
        target = self.base / "existing"
        target.mkdir()
        note = target / "keep.txt"
        note.write_text("keep me\n", encoding="utf-8")
        initialize_project(target, "existing", "Existing", ACTOR)
        self.assertEqual(note.read_text(encoding="utf-8"), "keep me\n")

    def test_missing_portfolio_fails_minimal_validation(self) -> None:
        (self.root / "portfolio.toml").unlink()
        report = validate(self.root, "minimal")
        codes = {issue["code"] for issue in report["errors"]}
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("project_file", codes)
        self.assertIn("portfolio_singleton", codes)

    def test_track_metadata_is_toml_escaped(self) -> None:
        path = new_track(
            self.root,
            "quoted-track",
            'A "quoted" title',
            'actor "quoted"',
        )
        track = load_toml(path / "track.toml")
        self.assertEqual(track["title"], 'A "quoted" title')
        self.assertEqual(track["created_by"], 'actor "quoted"')
        covered_report = validate(self.root, "minimal")
        self.assertEqual(covered_report["status"], "PASS", covered_report)


class RecordValidationTests(ProjectCase):
    def test_hash_text_rejects_path_traversal_identifiers(self) -> None:
        self.new_track()
        with self.assertRaisesRegex(ValueError, "prompt-id"):
            _hash_text(self.root, "track-one", "prompt", "../../../../victim")

    def test_provider_fixture_and_seal_tamper_detection(self) -> None:
        self.new_track()
        self.seal_texts()
        path = register_generation(
            self.root,
            track_id="track-one",
            actor=ACTOR,
            provider="example-provider",
            operation="create",
            occurred_at="2026-08-10T10:00:00+08:00",
            model="fixture-model",
            object_id="fixture-1",
            plan="",
            prompt_ref="prompt:p001",
            lyrics_ref="lyrics:l001",
            terms_snapshot_ref="",
            parent_refs=[],
            input_refs=[],
            output_refs=[],
            provider_data=["fixture_strength=2"],
        )
        self.assertEqual(validate(self.root, "post")["status"], "PASS")
        record = load_toml(path)
        record["model"] = "tampered"
        atomic_write_toml(path, record)
        report = validate(self.root, "minimal")
        self.assertIn("seal_mismatch", {issue["code"] for issue in report["errors"]})

    def test_missing_lineage_reference_fails(self) -> None:
        self.new_track()
        self.seal_texts()
        with self.assertRaisesRegex(LedgerError, "missing, ambiguous, or wrong type"):
            register_generation(
                self.root,
                track_id="track-one",
                actor=ACTOR,
                provider="example-provider",
                operation="create",
                occurred_at="2026-08-10T10:00:00+08:00",
                model="fixture-model",
                object_id="fixture-2",
                plan="",
                prompt_ref="prompt:p001",
                lyrics_ref="lyrics:l001",
                terms_snapshot_ref="",
                parent_refs=["generation:g999"],
                input_refs=[],
                output_refs=[],
                provider_data=[],
            )
        self.assertFalse((self.root / "tracks/track-one/generations/g001.toml").exists())

    def test_canonical_records_reject_credential_fields(self) -> None:
        self.new_track()
        track_path = self.root / "tracks" / "track-one" / "track.toml"
        track = load_toml(track_path)
        track["suno_api_key"] = "must-not-be-committed"
        track["client_secret"] = "also-forbidden"
        atomic_write_toml(track_path, track)
        report = validate(self.root, "minimal")
        credential_errors = [
            issue for issue in report["errors"] if issue["code"] == "credential_field"
        ]
        self.assertEqual(len(credential_errors), 2)

    def test_malformed_and_missing_path_references_fail(self) -> None:
        self.new_track()
        self.seal_texts()
        path = register_generation(
            self.root,
            track_id="track-one",
            actor=ACTOR,
            provider="suno",
            operation="create",
            occurred_at="2026-08-10T10:00:00+08:00",
            model="fixture-model",
            object_id="fixture-suno",
            plan="paid",
            prompt_ref="prompt:p001",
            lyrics_ref="lyrics:l001",
            terms_snapshot_ref="policies/platforms/suno/2026-08-10.toml",
            parent_refs=[],
            input_refs=[],
            output_refs=[],
            provider_data=[],
        )
        generation = load_toml(path)
        generation["prompt_ref"] = "not-a-reference"
        generation["terms_snapshot_ref"] = "policies/platforms/suno/does-not-exist.toml"
        atomic_write_toml(path, seal_record(generation))
        report = validate(self.root, "post")
        codes = {issue["code"] for issue in report["errors"]}
        self.assertIn("malformed_ref", codes)
        self.assertIn("terms_snapshot", codes)

    def test_configuration_and_provider_profile_errors_are_structured(self) -> None:
        config_path = self.root / "music.toml"
        original_config = config_path.read_text(encoding="utf-8")
        config_path.write_text("not = [valid\n", encoding="utf-8")
        report = validate(self.root, "minimal")
        self.assertIn("config_parse", {issue["code"] for issue in report["errors"]})

        config_path.write_text(original_config, encoding="utf-8")
        profile = self.root / "providers" / "suno" / "profile.toml"
        profile.write_text("not = [valid\n", encoding="utf-8")
        report = validate(self.root, "minimal")
        self.assertIn("provider_profile", {issue["code"] for issue in report["errors"]})

    def test_valid_toml_with_wrong_table_types_returns_structured_failure(self) -> None:
        config_path = self.root / "music.toml"
        config_path.write_text(
            'schema_version = "1.0"\n'
            'template_version = "0.2.0"\n'
            'tool_version = "0.2.0"\n'
            'project = "not-a-table"\n'
            'candidate_retention = "not-a-table"\n'
            'storage = "not-a-table"\n'
            'import_limits = "not-a-table"\n',
            encoding="utf-8",
        )
        report = validate(self.root, "minimal")
        self.assertEqual(report["status"], "FAIL")
        codes = {issue["code"] for issue in report["errors"]}
        self.assertIn("config_project", codes)
        self.assertIn("config_retention", codes)
        self.assertIn("config_storage", codes)

    def test_cli_inherits_project_provider_when_track_has_no_override(self) -> None:
        config_path = self.root / "music.toml"
        config = load_toml(config_path)
        config["project"]["default_provider"] = "example-provider"
        atomic_write_toml(config_path, config)
        self.new_track()
        self.seal_texts()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = cli_main(
                [
                    "--root",
                    str(self.root),
                    "register-generation",
                    "--track-id",
                    "track-one",
                    "--submission-id",
                    str(uuid.uuid4()),
                    "--submitted-by-type",
                    "human",
                    "--submitted-by-id",
                    ACTOR,
                    "--model",
                    "fixture-model",
                    "--object-id",
                    "configured-provider",
                    "--prompt-ref",
                    "prompt:p001",
                    "--lyrics-ref",
                    "lyrics:l001",
                    "--confirm",
                ]
            )
        self.assertEqual(result, 0)
        event = load_toml(self.root / "tracks" / "track-one" / "generations" / "g001.toml")
        self.assertEqual(event["provider"], "example-provider")

    def test_immutable_event_cannot_drop_its_seal(self) -> None:
        self.new_track()
        self.seal_texts()
        path = register_generation(
            self.root,
            track_id="track-one",
            actor=ACTOR,
            provider="example-provider",
            operation="create",
            occurred_at="2026-08-10T10:00:00+08:00",
            model="fixture-model",
            object_id="fixture-unsealed",
            plan="",
            prompt_ref="prompt:p001",
            lyrics_ref="lyrics:l001",
            terms_snapshot_ref="",
            parent_refs=[],
            input_refs=[],
            output_refs=[],
            provider_data=[],
        )
        event = load_toml(path)
        event.pop("seal")
        event["status"] = "draft"
        atomic_write_toml(path, event)
        report = validate(self.root, "minimal")
        self.assertIn("seal_required", {issue["code"] for issue in report["errors"]})

    def test_terms_snapshot_is_provider_typed_and_content_bound(self) -> None:
        self.new_track()
        self.seal_texts()
        path = register_generation(
            self.root,
            track_id="track-one",
            actor=ACTOR,
            provider="suno",
            operation="create",
            occurred_at="2026-08-10T10:00:00+08:00",
            model="fixture-model",
            object_id="fixture-policy",
            plan="paid",
            prompt_ref="prompt:p001",
            lyrics_ref="lyrics:l001",
            terms_snapshot_ref="policies/platforms/suno/2026-08-10.toml",
            parent_refs=[],
            input_refs=[],
            output_refs=[],
            provider_data=[],
        )
        self.assertTrue(load_toml(path)["terms_snapshot_sha256"])
        policy = self.root / "policies" / "platforms" / "suno" / "2026-08-10.toml"
        policy.write_text(policy.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        report = validate(self.root, "minimal")
        self.assertIn("terms_snapshot", {issue["code"] for issue in report["errors"]})

    def test_generation_normalizes_an_absolute_policy_snapshot_path(self) -> None:
        self.new_track()
        self.seal_texts()
        path = register_generation(
            self.root,
            track_id="track-one",
            actor=ACTOR,
            provider="suno",
            operation="create",
            occurred_at="2026-08-10T10:00:00+08:00",
            model="fixture-model",
            object_id="absolute-policy",
            plan="paid",
            prompt_ref="prompt:p001",
            lyrics_ref="lyrics:l001",
            terms_snapshot_ref=str(
                (self.root / "policies/platforms/suno/2026-08-10.toml").resolve()
            ),
            parent_refs=[],
            input_refs=[],
            output_refs=[],
            provider_data=[],
        )
        self.assertEqual(
            load_toml(path)["terms_snapshot_ref"],
            "policies/platforms/suno/2026-08-10.toml",
        )

    def test_malformed_rights_seal_is_a_structured_failure(self) -> None:
        self.new_track()
        self.seal_texts()
        source = self.base / "input.wav"
        source.write_bytes(b"RIFF-input")
        asset_ref = safe_import(
            self.root,
            source,
            bucket="inputs",
            track_id="track-one",
            actor=ACTOR,
            role="provider_input",
        )[0]["asset_ref"]
        register_generation(
            self.root,
            track_id="track-one",
            actor=ACTOR,
            provider="suno",
            operation="create",
            occurred_at="2026-08-10T10:00:00+08:00",
            model="fixture-model",
            object_id="malformed-rights",
            plan="paid",
            prompt_ref="prompt:p001",
            lyrics_ref="lyrics:l001",
            terms_snapshot_ref="policies/platforms/suno/2026-08-10.toml",
            parent_refs=[],
            input_refs=[asset_ref],
            output_refs=[],
            provider_data=[],
        )
        rights_path = record_rights(
            self.root,
            track_id="track-one",
            actor=ACTOR,
            subject_ref="generation:g001",
            source_type="uploaded_audio",
            human_status="confirmed",
            private_locator="private-ledger:input",
            evidence_sha256="2" * 64,
            public_note="fixture",
        )
        rights = load_toml(rights_path)
        rights["seal"] = "bad"
        atomic_write_toml(rights_path, rights)
        report = validate(self.root, "minimal")
        codes = {issue["code"] for issue in report["errors"]}
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("seal_mismatch", codes)
        self.assertIn(
            "provider_rights", {issue["code"] for issue in report["warnings"]}
        )

    def test_provider_rights_rules_require_event_coverage(self) -> None:
        self.new_track()
        self.seal_texts()
        register_generation(
            self.root,
            track_id="track-one",
            actor=ACTOR,
            provider="suno",
            operation="create",
            occurred_at="2026-08-10T10:00:00+08:00",
            model="fixture-model",
            object_id="fixture-voice",
            plan="paid",
            prompt_ref="prompt:p001",
            lyrics_ref="lyrics:l001",
            terms_snapshot_ref="policies/platforms/suno/2026-08-10.toml",
            parent_refs=[],
            input_refs=[],
            output_refs=[],
            provider_data=["voice=fixture-voice"],
        )
        report = validate(self.root, "minimal")
        self.assertEqual(report["status"], "PASS", report)
        self.assertIn("provider_rights", {issue["code"] for issue in report["warnings"]})
        record_rights(
            self.root,
            track_id="track-one",
            actor=ACTOR,
            subject_ref="generation:g001",
            source_type="voice_consent",
            human_status="confirmed",
            private_locator="private-ledger:voice-1",
            evidence_sha256="1" * 64,
            public_note="fixture",
        )
        covered_report = validate(self.root, "minimal")
        self.assertEqual(covered_report["status"], "PASS", covered_report)

    def test_confirmed_rights_require_evidence_locator_and_hash(self) -> None:
        self.new_track()
        with self.assertRaisesRegex(LedgerError, "evidence_ref"):
            record_rights(
                self.root,
                track_id="track-one",
                actor=ACTOR,
                subject_ref="track:track-one",
                source_type="original",
                human_status="confirmed",
                private_locator="",
                evidence_sha256="",
                public_note="",
            )

    def test_record_ids_continue_beyond_three_digits(self) -> None:
        directory = self.root / "tracks" / "_template" / "generations"
        atomic_write_toml(directory / "g999.toml", {"record_type": "generation"})
        atomic_write_toml(directory / "g1000.toml", {"record_type": "generation"})
        self.assertEqual(_next_id(directory, "g", "generation"), "g1001")


class EvidenceEventTests(ProjectCase):
    def generation_envelope(self, submission_id: str | None = None) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "event_type": "generation",
            "submission_id": submission_id or str(uuid.uuid4()),
            "submitted_by": {"type": "agent", "id": "studio-agent"},
            "track_id": "track-one",
            "payload": {
                "provider": "example-provider",
                "operation": "create",
                "occurred_at": "2026-08-10T10:00:00+08:00",
                "model": "fixture-model",
                "object_id": "fixture-object",
                "prompt_ref": "prompt:p001",
                "lyrics_ref": "lyrics:l001",
                "parent_refs": [],
                "input_refs": [],
                "output_refs": [],
                "provider_data": {"fixture_strength": 2},
            },
        }

    def setUp(self) -> None:
        super().setUp()
        self.new_track()
        self.seal_texts()

    def test_submission_is_idempotent_and_sealed_record_is_the_receipt(self) -> None:
        envelope = self.generation_envelope()
        first = apply_event(self.root, envelope, confirmed=True)
        second = apply_event(self.root, envelope, confirmed=True)
        self.assertEqual(first["record_path"], second["record_path"])
        self.assertEqual(second["status"], "idempotent")
        record = load_toml(self.root / str(first["record_path"]))
        self.assertEqual(record["submission_id"], envelope["submission_id"])
        self.assertEqual(record["submitted_by"], envelope["submitted_by"])
        self.assertEqual(record["output_binding"]["record_path"], first["record_path"])
        self.assertTrue(record["seal"]["sealed"])
        self.assertEqual(len(list((self.root / "tracks/track-one/generations").glob("g*.toml"))), 1)

    def test_submission_conflict_fails_closed(self) -> None:
        envelope = self.generation_envelope()
        apply_event(self.root, envelope, confirmed=True)
        changed = json.loads(json.dumps(envelope))
        changed["payload"]["object_id"] = "different-object"
        with self.assertRaisesRegex(LedgerError, "different envelope digest") as caught:
            apply_event(self.root, changed, confirmed=True)
        self.assertEqual(caught.exception.code, "submission_conflict")

    def test_idempotency_rejects_a_tampered_authoritative_record(self) -> None:
        envelope = self.generation_envelope()
        result = apply_event(self.root, envelope, confirmed=True)
        path = self.root / str(result["record_path"])
        record = load_toml(path)
        record["model"] = "tampered"
        atomic_write_toml(path, record)
        with self.assertRaises(LedgerError) as caught:
            apply_event(self.root, envelope, confirmed=True)
        self.assertEqual(caught.exception.code, "ledger_invalid")

    def test_submission_id_must_use_canonical_lowercase_uuid(self) -> None:
        envelope = self.generation_envelope()
        envelope["submission_id"] = str(envelope["submission_id"]).upper()
        with self.assertRaises(LedgerError) as caught:
            apply_event(self.root, envelope, confirmed=True)
        self.assertEqual(caught.exception.code, "invalid_envelope")

    def test_ordinary_plan_is_informational_and_apply_revalidates(self) -> None:
        envelope = self.generation_envelope()
        plan = plan_event(self.root, envelope)
        self.assertFalse(plan["binding_required"])
        self.assertNotIn("plan_digest", plan)
        result = apply_event(self.root, envelope, confirmed=True)
        self.assertEqual(result["status"], "applied")

    def test_lock_timeout_returns_stable_ledger_busy_error(self) -> None:
        with LedgerLock(self.root, timeout=0):
            with self.assertRaises(LedgerError) as caught:
                plan_event(self.root, self.generation_envelope(), timeout=0)
        self.assertEqual(caught.exception.code, "ledger_busy")
        self.assertTrue((self.root / ".music-ledger.lock").is_file())

    def test_rights_evidence_rejects_private_locator_shapes(self) -> None:
        base = {
            "schema_version": "1.0",
            "event_type": "rights_evidence",
            "submission_id": str(uuid.uuid4()),
            "submitted_by": {"type": "agent", "id": "studio-agent"},
            "assessed_by": {"type": "human", "id": ACTOR},
            "track_id": "track-one",
            "payload": {
                "subject_ref": "track:track-one",
                "source_type": "original",
                "human_status": "confirmed",
                "evidence_ref": "/Users/private/receipt.pdf",
                "evidence_sha256": "a" * 64,
            },
        }
        for unsafe in (
            "/Users/private/receipt.pdf",
            "file:///Users/private/receipt.pdf",
            "https://user:pass@example.com/receipt",
            "https://example.com/receipt?token=secret",
        ):
            envelope = json.loads(json.dumps(base))
            envelope["submission_id"] = str(uuid.uuid4())
            envelope["payload"]["evidence_ref"] = unsafe
            with self.assertRaises(LedgerError) as caught:
                apply_event(self.root, envelope, confirmed=True)
            self.assertEqual(caught.exception.code, "unsafe_evidence_ref")

    def test_domain_responsibility_must_be_declared_human(self) -> None:
        envelope = {
            "schema_version": "1.0",
            "event_type": "review",
            "submission_id": str(uuid.uuid4()),
            "submitted_by": {"type": "agent", "id": "studio-agent"},
            "reviewed_by": {"type": "agent", "id": "studio-agent"},
            "track_id": "track-one",
            "payload": {"subject_ref": "track:track-one", "decision": "approved"},
        }
        with self.assertRaisesRegex(LedgerError, "reviewed_by"):
            apply_event(self.root, envelope, confirmed=True)

    def test_optional_payload_fields_reject_non_scalar_shapes(self) -> None:
        envelope = self.generation_envelope()
        envelope["payload"]["provider_data"] = {"fixture_strength": [2]}
        with self.assertRaises(LedgerError) as caught:
            apply_event(self.root, envelope, confirmed=True)
        self.assertEqual(caught.exception.code, "invalid_envelope")

    def test_unsupported_schema_is_structured_and_not_migrated(self) -> None:
        config_path = self.root / "music.toml"
        config = load_toml(config_path)
        config["schema_version"] = "0.1"
        atomic_write_toml(config_path, config)
        stderr = StringIO()
        with redirect_stderr(stderr):
            result = cli_main(["--root", str(self.root), "status"])
        error = json.loads(stderr.getvalue())
        self.assertEqual(result, 2)
        self.assertEqual(error["error"]["code"], "unsupported_schema")
        self.assertEqual(load_toml(config_path)["schema_version"], "0.1")

    def test_event_cli_requires_explicit_root(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            result = cli_main(["plan-event", "event.json"])
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "root_required")

    def test_ordinary_write_failure_leaves_no_authoritative_record(self) -> None:
        with patch(
            "scripts.music_project.events.atomic_write_toml",
            side_effect=OSError("fixture write failure"),
        ):
            with self.assertRaisesRegex(OSError, "fixture write failure"):
                apply_event(self.root, self.generation_envelope(), confirmed=True)
        self.assertEqual(
            list((self.root / "tracks/track-one/generations").glob("g*.toml")), []
        )


class ImportTests(ProjectCase):
    def test_regular_asset_import_is_hashed_and_lfs_covered(self) -> None:
        self.new_track()
        self.new_track("track-two")
        self.seal_texts()
        self.seal_texts("track-two")
        source = self.base / "source.wav"
        source.write_bytes(b"RIFF-fixture-audio")
        imported = safe_import(
            self.root,
            source,
            bucket="selected",
            track_id="track-one",
            actor=ACTOR,
            role="selected_source",
        )
        self.assertEqual(imported[0]["sha256"], sha256_file(source))
        self.assertEqual(imported[0]["asset_ref"], f"asset:a-{sha256_file(source)}")
        self.assertEqual(
            imported[0]["asset_record"],
            f"assets/records/a-{sha256_file(source)}.toml",
        )
        self.assertTrue(
            imported[0]["asset_location_record"].startswith(
                "assets/records/locations/al-"
            )
        )
        second = safe_import(
            self.root,
            source,
            bucket="selected",
            track_id="track-two",
            actor=ACTOR,
            role="selected_source",
        )
        self.assertEqual(second[0]["asset_ref"], imported[0]["asset_ref"])
        self.assertEqual(
            len(list((self.root / "assets" / "records").glob("a-*.toml"))), 1
        )
        self.assertEqual(
            len(list((self.root / "assets" / "records" / "locations").glob("al-*.toml"))),
            2,
        )
        report = validate(self.root, "post")
        self.assertEqual(report["status"], "PASS", report)

    def test_lfs_rules_are_scoped_to_versioned_asset_buckets(self) -> None:
        covered = subprocess.run(
            ["git", "check-attr", "filter", "--", "assets/selected/a.wav"],
            cwd=self.root,
            text=True,
            check=True,
            capture_output=True,
        ).stdout
        unrelated = subprocess.run(
            ["git", "check-attr", "filter", "--", "tracks/demo/renders/temp.wav"],
            cwd=self.root,
            text=True,
            check=True,
            capture_output=True,
        ).stdout
        self.assertIn("filter: lfs", covered)
        self.assertIn("filter: unspecified", unrelated)

    def test_provider_export_is_a_first_class_lineage_event(self) -> None:
        self.new_track()
        self.seal_texts()
        source = self.base / "source.wav"
        source.write_bytes(b"RIFF-export-source")
        imported = safe_import(
            self.root,
            source,
            bucket="selected",
            track_id="track-one",
            actor=ACTOR,
            role="selected_source",
        )[0]
        register_generation(
            self.root,
            track_id="track-one",
            actor=ACTOR,
            provider="example-provider",
            operation="create",
            occurred_at="2026-08-10T10:00:00+08:00",
            model="fixture-model",
            object_id="fixture-export",
            plan="",
            prompt_ref="prompt:p001",
            lyrics_ref="lyrics:l001",
            terms_snapshot_ref="",
            parent_refs=[],
            input_refs=[],
            output_refs=[imported["asset_ref"]],
            provider_data=[],
        )
        path = register_export(
            self.root,
            track_id="track-one",
            actor=ACTOR,
            provider="example-provider",
            operation="download",
            occurred_at="2026-08-10T10:05:00+08:00",
            parent_refs=["generation:g001"],
            input_refs=[imported["asset_ref"]],
            output_refs=[imported["asset_ref"]],
            provider_data=[],
        )
        self.assertEqual(path.name, "x001.toml")
        self.assertEqual(load_toml(path)["record_type"], "export")
        report = validate(self.root, "post")
        self.assertEqual(report["status"], "PASS", report)

    def test_zip_traversal_and_symlink_are_rejected(self) -> None:
        self.new_track()
        traversal = self.base / "traversal.zip"
        with zipfile.ZipFile(traversal, "w") as archive:
            archive.writestr("../escape.wav", b"bad")
        with self.assertRaisesRegex(ValueError, "unsafe ZIP member"):
            safe_import(
                self.root,
                traversal,
                bucket="inputs",
                track_id="track-one",
                actor=ACTOR,
                role="input",
            )

        symlink = self.base / "symlink.zip"
        info = zipfile.ZipInfo("link.wav")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        with zipfile.ZipFile(symlink, "w") as archive:
            archive.writestr(info, "target.wav")
        with self.assertRaisesRegex(ValueError, "symlink"):
            safe_import(
                self.root,
                symlink,
                bucket="inputs",
                track_id="track-one",
                actor=ACTOR,
                role="input",
            )

    def test_import_rejects_symlinked_destination_root(self) -> None:
        self.new_track()
        outside = self.base / "outside-import"
        outside.mkdir()
        destination = self.root / "assets" / "selected" / "track-one"
        destination.symlink_to(outside, target_is_directory=True)
        source = self.base / "source.wav"
        source.write_bytes(b"must-stay-inside")
        with self.assertRaisesRegex(ValueError, "escapes repository"):
            safe_import(
                self.root,
                source,
                bucket="selected",
                track_id="track-one",
                actor=ACTOR,
                role="selected_source",
            )
        self.assertFalse((outside / "source.wav").exists())


class RetentionTests(ProjectCase):
    def test_default_and_track_override_are_configuration_driven(self) -> None:
        self.new_track()
        self.assertEqual(effective_policy(self.root, "track-one")["days"], 90)
        track_path = self.root / "tracks" / "track-one" / "track.toml"
        track = load_toml(track_path)
        track["candidate_retention"] = {
            "inherit": False,
            "mode": "timed_local",
            "days": 1,
            "archive_locator": "",
        }
        atomic_write_toml(track_path, track)
        self.assertEqual(effective_policy(self.root, "track-one")["days"], 1)
        track["candidate_retention"]["days"] = 3
        atomic_write_toml(track_path, track)
        self.assertEqual(effective_policy(self.root, "track-one")["days"], 3)

    def test_apply_requires_confirmation_and_revalidates_scope(self) -> None:
        self.new_track()
        config_path = self.root / "music.toml"
        config = load_toml(config_path)
        config["candidate_retention"]["days"] = 0
        atomic_write_toml(config_path, config)
        candidate = self.root / "assets" / "candidates-local" / "track-one" / "lost.wav"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        plan = build_plan(self.root, "track-one")
        self.assertTrue(plan["entries"][0]["eligible"])
        with self.assertRaisesRegex(ValueError, "confirmation"):
            apply_plan(self.root, plan, ACTOR)
        result = apply_plan(self.root, plan, ACTOR, confirmed=True)
        self.assertEqual(len(result["applied"]), 1)
        self.assertFalse(candidate.exists())

        malicious = {
            "entries": [
                {
                    "eligible": True,
                    "track_id": "track-one",
                    "path": "music.toml",
                    "sha256": sha256_file(config_path),
                    "action": "record_then_delete",
                }
            ]
        }
        with self.assertRaisesRegex(ValueError, "outside the candidate area"):
            apply_plan(self.root, malicious, ACTOR, confirmed=True)
        self.assertTrue(config_path.exists())

    def test_external_archive_must_not_overlap_candidate_storage(self) -> None:
        self.new_track()
        config_path = self.root / "music.toml"
        config = load_toml(config_path)
        config["candidate_retention"] = {
            "mode": "external_archive",
            "days": 0,
            "archive_locator": str(self.root / "assets" / "candidates-local"),
        }
        atomic_write_toml(config_path, config)
        candidate = self.root / "assets" / "candidates-local" / "track-one" / "only.wav"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"only-copy")
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            build_plan(self.root, "track-one")
        self.assertTrue(candidate.exists())

    def test_stale_plan_is_fully_preflighted_before_deletion(self) -> None:
        self.new_track()
        config_path = self.root / "music.toml"
        config = load_toml(config_path)
        config["candidate_retention"]["days"] = 0
        atomic_write_toml(config_path, config)
        directory = self.root / "assets" / "candidates-local" / "track-one"
        directory.mkdir(parents=True)
        first = directory / "first.wav"
        second = directory / "second.wav"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        plan = build_plan(self.root, "track-one")
        second.write_bytes(b"changed-after-plan")
        with self.assertRaisesRegex(RuntimeError, "changed after planning"):
            apply_plan(self.root, plan, ACTOR, confirmed=True)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_retention_report_cannot_overwrite_canonical_files(self) -> None:
        portfolio = self.root / "portfolio.toml"
        before = portfolio.read_bytes()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            rejected = cli_main(
                [
                    "--root",
                    str(self.root),
                    "retention-plan",
                    "--output",
                    "portfolio.toml",
                ]
            )
            written = cli_main(
                [
                    "--root",
                    str(self.root),
                    "retention-plan",
                    "--output",
                    "reports/retention/plan.json",
                ]
            )
            duplicate = cli_main(
                [
                    "--root",
                    str(self.root),
                    "retention-plan",
                    "--output",
                    "reports/retention/plan.json",
                ]
            )
        self.assertEqual(rejected, 2)
        self.assertEqual(written, 0)
        self.assertEqual(duplicate, 2)
        self.assertEqual(portfolio.read_bytes(), before)

    def test_archive_preflight_disambiguates_duplicate_basenames(self) -> None:
        self.new_track()
        archive = self.base / "cold-storage"
        config_path = self.root / "music.toml"
        config = load_toml(config_path)
        config["candidate_retention"] = {
            "mode": "external_archive",
            "days": 0,
            "archive_locator": str(archive),
        }
        atomic_write_toml(config_path, config)
        candidate_root = self.root / "assets" / "candidates-local" / "track-one"
        first = candidate_root / "one" / "same.wav"
        second = candidate_root / "two" / "same.wav"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_bytes(b"first-content")
        second.write_bytes(b"second-content")
        plan = build_plan(self.root, "track-one")
        result = apply_plan(self.root, plan, ACTOR, confirmed=True)
        archived = list((archive / "track-one").glob("*same.wav"))
        self.assertEqual(len(result["applied"]), 2)
        self.assertEqual(len(archived), 2)
        self.assertEqual(
            {path.read_bytes() for path in archived},
            {b"first-content", b"second-content"},
        )
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_retention_rejects_policy_drift_and_unknown_tracks(self) -> None:
        self.new_track()
        config_path = self.root / "music.toml"
        config = load_toml(config_path)
        config["candidate_retention"] = {
            "mode": "external_archive",
            "days": 0,
            "archive_locator": str(self.base / "archive-a"),
        }
        atomic_write_toml(config_path, config)
        candidate = self.root / "assets" / "candidates-local" / "track-one" / "candidate.wav"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        plan = build_plan(self.root, "track-one")
        config["candidate_retention"]["archive_locator"] = str(self.base / "archive-b")
        atomic_write_toml(config_path, config)
        with self.assertRaisesRegex(ValueError, "policy changed"):
            apply_plan(self.root, plan, ACTOR, confirmed=True)
        self.assertTrue(candidate.exists())

        ghost = self.root / "assets" / "candidates-local" / "track-typo" / "lost.wav"
        ghost.parent.mkdir(parents=True)
        ghost.write_bytes(b"lost")
        with self.assertRaisesRegex(FileNotFoundError, "unknown track"):
            build_plan(self.root)

    def test_relative_archive_is_resolved_from_project_root(self) -> None:
        self.new_track()
        config_path = self.root / "music.toml"
        config = load_toml(config_path)
        config["candidate_retention"] = {
            "mode": "external_archive",
            "days": 0,
            "archive_locator": "../relative-cold",
        }
        atomic_write_toml(config_path, config)
        candidate = self.root / "assets" / "candidates-local" / "track-one" / "candidate.wav"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        plan = build_plan(self.root, "track-one")
        expected = self.root.parent / "relative-cold" / "track-one"
        self.assertEqual(
            Path(plan["entries"][0]["policy"]["resolved_archive_directory"]),
            expected.resolve(),
        )

    def test_retention_quarantines_and_rechecks_before_deletion(self) -> None:
        self.new_track()
        config_path = self.root / "music.toml"
        config = load_toml(config_path)
        config["candidate_retention"]["days"] = 0
        atomic_write_toml(config_path, config)
        candidate = self.root / "assets" / "candidates-local" / "track-one" / "candidate.wav"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"planned")
        plan = build_plan(self.root, "track-one")
        real_replace = os.replace

        def tampering_replace(source: object, destination: object) -> None:
            real_replace(source, destination)
            destination_path = Path(destination)
            if "retention-" in destination_path.as_posix():
                destination_path.write_bytes(b"changed-during-apply")

        with patch("scripts.music_project.retention.os.replace", side_effect=tampering_replace):
            with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                apply_plan(self.root, plan, ACTOR, confirmed=True)
        self.assertTrue(candidate.exists())

    def test_retention_rolls_back_the_entire_batch_when_recording_fails(self) -> None:
        self.new_track()
        config_path = self.root / "music.toml"
        config = load_toml(config_path)
        config["candidate_retention"]["days"] = 0
        atomic_write_toml(config_path, config)
        directory = self.root / "assets" / "candidates-local" / "track-one"
        directory.mkdir(parents=True)
        first = directory / "first.wav"
        second = directory / "second.wav"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        plan = build_plan(self.root, "track-one")
        calls = 0

        def fail_second_record(*args: object, **kwargs: object) -> tuple[str, Path]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("fixture record failure")
            return create_asset_record(*args, **kwargs)

        with patch(
            "scripts.music_project.retention.create_asset_record",
            side_effect=fail_second_record,
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture record failure"):
                apply_plan(self.root, plan, ACTOR, confirmed=True)
        self.assertEqual(first.read_bytes(), b"first")
        self.assertEqual(second.read_bytes(), b"second")
        self.assertEqual(list((self.root / "assets" / "records").glob("a-*.toml")), [])
        self.assertEqual(
            list((self.root / "assets" / "records" / "locations").glob("al-*.toml")),
            [],
        )

    def test_retention_rollback_preserves_preexisting_location_evidence(self) -> None:
        self.new_track()
        config_path = self.root / "music.toml"
        config = load_toml(config_path)
        config["candidate_retention"]["days"] = 0
        atomic_write_toml(config_path, config)
        directory = self.root / "assets" / "candidates-local" / "track-one"
        directory.mkdir(parents=True)
        first = directory / "first.wav"
        second = directory / "second.wav"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        _, existing_location = create_asset_record(
            self.root,
            source=first,
            sha256=sha256_file(first),
            size_bytes=first.stat().st_size,
            actor=ACTOR,
            role="rejected_candidate",
            storage="metadata_only",
            track_ref="track:track-one",
            original_name=first.name,
        )
        plan = build_plan(self.root, "track-one")
        calls = 0

        def fail_second_record(*args: object, **kwargs: object) -> tuple[str, Path]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("fixture record failure")
            return create_asset_record(*args, **kwargs)

        with patch(
            "scripts.music_project.retention.create_asset_record",
            side_effect=fail_second_record,
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture record failure"):
                apply_plan(self.root, plan, ACTOR, confirmed=True)
        self.assertTrue(existing_location.is_file())
        self.assertEqual(first.read_bytes(), b"first")
        self.assertEqual(second.read_bytes(), b"second")


class ReleaseTests(ProjectCase):
    def setUp(self) -> None:
        super().setUp()
        self.new_track()
        self.seal_texts()
        self.master = self.base / "master.wav"
        with wave.open(str(self.master), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(44100)
            output.writeframes(b"\0\0" * 4410)
        self.review_ref = self.approved_review(
            subject_sha256=sha256_file(self.master)
        )
        self.policy_ref = "policies/platforms/suno/2026-08-10.toml"

    def freeze(self, rights_refs: list[str], **overrides: object) -> tuple[Path, str]:
        options = {
            "override_reason": "",
            "override_risks": [],
        }
        options.update(overrides)
        return freeze_release(
            self.root,
            track_id="track-one",
            master=self.master,
            title="Fixture Release",
            actor=ACTOR,
            listening_gate_refs=[self.review_ref],
            rights_refs=rights_refs,
            policy_snapshot_refs=[self.policy_ref],
            release_id="rc001",
            confirmed=True,
            **options,
        )

    def release_envelope(
        self, rights_refs: list[str], override_reason: str = ""
    ) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "event_type": "release_candidate",
            "submission_id": str(uuid.uuid4()),
            "submitted_by": {"type": "agent", "id": "studio-agent"},
            "confirmed_by": {"type": "human", "id": ACTOR},
            "track_id": "track-one",
            "payload": {
                "master": str(self.master),
                "title": "Fixture Release",
                "release_id": "rc001",
                "listening_gate_refs": [self.review_ref],
                "rights_refs": rights_refs,
                "policy_snapshot_refs": [self.policy_ref],
                "override_reason": override_reason,
            },
        }

    def test_confirmed_rights_freeze_and_publication(self) -> None:
        rc_path, gate = self.freeze([self.rights("confirmed")])
        self.assertEqual(gate, "PASS")
        self.assertTrue(rc_path.is_file())
        report = validate(self.root, "release")
        self.assertEqual(report["status"], "PASS", report)
        digest = sha256_file(self.master)
        publication = record_publication(
            self.root,
            release_id="rc001",
            actor=ACTOR,
            release_url="https://github.com/example/project/releases/tag/release%2Frc001",
            asset_url="https://github.com/example/project/releases/download/release%2Frc001/master.wav",
            asset_name="master.wav",
            asset_sha256=digest,
            confirmed=True,
        )
        self.assertTrue(publication.is_file())
        report = validate(self.root, "release")
        self.assertEqual(report["status"], "PASS", report)
        with self.assertRaisesRegex(ValueError, "rcNNN"):
            record_publication(
                self.root,
                release_id="../../outside",
                actor=ACTOR,
                release_url="https://github.com/example/project/releases/tag/bad",
                asset_url="https://github.com/example/project/releases/download/bad/master.wav",
                asset_name="master.wav",
                asset_sha256=digest,
                confirmed=True,
            )

    def test_release_requires_matching_plan_and_is_idempotent_after_commit(self) -> None:
        envelope = self.release_envelope([self.rights("confirmed")])
        receipt = plan_event(self.root, envelope)
        applied = apply_event(
            self.root, envelope, confirmed=True, plan_receipt=receipt
        )
        repeated = apply_event(self.root, envelope, confirmed=True)
        self.assertEqual(applied["record_path"], repeated["record_path"])
        self.assertEqual(repeated["status"], "idempotent")
        self.assertEqual(
            applied["record"]["plan_digest"], receipt["plan_digest"]
        )

    def test_release_plan_fails_closed_after_relevant_ledger_drift(self) -> None:
        envelope = self.release_envelope([self.rights("confirmed")])
        receipt = plan_event(self.root, envelope)
        self.approved_review(subject_sha256=sha256_file(self.master))
        with self.assertRaises(LedgerError) as caught:
            apply_event(self.root, envelope, confirmed=True, plan_receipt=receipt)
        self.assertEqual(caught.exception.code, "plan_stale")
        self.assertFalse((self.root / "releases/rc001").exists())

    def test_release_plan_receipt_rejects_content_tampering(self) -> None:
        envelope = self.release_envelope([self.rights("confirmed")])
        receipt = plan_event(self.root, envelope)
        receipt["plan"]["expected_output"]["release_id"] = "rc999"
        with self.assertRaises(LedgerError) as caught:
            apply_event(self.root, envelope, confirmed=True, plan_receipt=receipt)
        self.assertEqual(caught.exception.code, "invalid_plan_receipt")
        self.assertFalse((self.root / "releases/rc001").exists())

    def test_release_staging_failure_leaves_no_partial_release(self) -> None:
        envelope = self.release_envelope([self.rights("confirmed")])
        receipt = plan_event(self.root, envelope)
        with patch(
            "scripts.music_project.events.analyze_audio",
            side_effect=RuntimeError("fixture analysis failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture analysis failure"):
                apply_event(self.root, envelope, confirmed=True, plan_receipt=receipt)
        self.assertFalse((self.root / "releases/rc001").exists())
        self.assertFalse(
            (self.root / "releases/.staging" / str(envelope["submission_id"])).exists()
        )

    def test_unconfirmed_rights_use_minimal_override_without_clearance_claim(self) -> None:
        envelope = self.release_envelope(
            [self.rights("needs_review")], "receipt is still pending"
        )
        receipt = plan_event(self.root, envelope)
        result = apply_event(
            self.root, envelope, confirmed=True, plan_receipt=receipt
        )
        record = result["record"]
        gate = load_toml(self.root / "releases/rc001/gate.toml")
        self.assertEqual(record["gate_status"], "PASS_WITH_OVERRIDE")
        self.assertEqual(record["override_reason"], "receipt is still pending")
        self.assertEqual(record["confirmed_by"], {"type": "human", "id": ACTOR})
        self.assertEqual(gate["override_reason"], "receipt is still pending")
        self.assertIn("not rights-cleared", gate["note"])

    def test_published_master_survives_staging_cleanup_and_future_freeze(self) -> None:
        rights_ref = self.rights("confirmed")
        self.freeze([rights_ref])
        digest = sha256_file(self.master)
        record_publication(
            self.root,
            release_id="rc001",
            actor=ACTOR,
            release_url="https://github.com/example/project/releases/tag/release%2Frc001",
            asset_url="https://github.com/example/project/releases/download/release%2Frc001/master.wav",
            asset_name="master.wav",
            asset_sha256=digest,
            confirmed=True,
        )
        self.assertFalse((self.root / "releases" / ".staging" / "rc001").exists())
        self.assertTrue((self.root / "releases" / "rc001" / "master" / "master.wav").is_file())
        report = validate(self.root, "post")
        self.assertEqual(report["status"], "PASS", report)
        rc_path, gate = freeze_release(
            self.root,
            track_id="track-one",
            master=self.master,
            title="Fixture Release Two",
            actor=ACTOR,
            listening_gate_refs=[self.review_ref],
            rights_refs=[rights_ref],
            policy_snapshot_refs=[self.policy_ref],
            release_id="rc002",
            override_reason="",
            override_risks=[],
            confirmed=True,
        )
        self.assertTrue(rc_path.is_file())
        self.assertEqual(gate, "PASS")

    def test_frozen_analysis_contains_no_workstation_path(self) -> None:
        self.freeze([self.rights("confirmed")])
        analysis = json.loads(
            (self.root / "releases" / "rc001" / "analysis.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn("source", analysis)
        self.assertNotIn(str(self.root), json.dumps(analysis))

    def test_incomplete_rights_need_structured_override(self) -> None:
        with self.assertRaisesRegex(LedgerError, "override_reason"):
            self.freeze([])
        _, gate = self.freeze(
            [],
            override_reason="source receipt is pending",
            override_risks=["licence document not yet public"],
        )
        self.assertEqual(gate, "PASS_WITH_OVERRIDE")

    def test_frozen_release_artifacts_are_required_and_hashed(self) -> None:
        self.freeze([self.rights("confirmed")])
        metadata = self.root / "releases" / "rc001" / "metadata.toml"
        metadata.unlink()
        policy = self.root / self.policy_ref
        policy.write_text(policy.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
        report = validate(self.root, "release")
        messages = "\n".join(issue["message"] for issue in report["errors"])
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("metadata.toml", messages)
        self.assertIn("metadata.toml", messages)
        self.assertIn("policy", messages)

    def test_release_validation_rejects_malformed_rights_refs(self) -> None:
        self.freeze([self.rights("confirmed")])
        rc_path = self.root / "releases" / "rc001" / "rc.toml"
        rc = load_toml(rc_path)
        rc["rights_refs"] = [1]
        atomic_write_toml(rc_path, seal_record(rc))
        report = validate(self.root, "release")
        codes = {issue["code"] for issue in report["errors"]}
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("malformed_ref", codes)
        self.assertIn("rights_gate", codes)
        self.assertIn("release_gate", codes)

    def test_known_unlicensed_is_not_overridable(self) -> None:
        evidence = self.rights("known_unlicensed")
        with self.assertRaisesRegex(LedgerError, "known_unlicensed"):
            self.freeze(
                [evidence],
                override_reason="attempted override",
                override_risks=["known issue"],
            )

    def test_omitted_known_unlicensed_evidence_still_blocks_freeze(self) -> None:
        self.rights("known_unlicensed")
        with self.assertRaisesRegex(LedgerError, "known_unlicensed"):
            self.freeze(
                [],
                override_reason="attempted omission",
                override_risks=["known issue"],
            )

    def test_listening_gate_must_identify_the_exact_master(self) -> None:
        wrong_review = self.approved_review(subject_sha256="f" * 64)
        with self.assertRaisesRegex(LedgerError, "not bound to the exact master"):
            freeze_release(
                self.root,
                track_id="track-one",
                master=self.master,
                title="Wrong Master Review",
                actor=ACTOR,
                listening_gate_refs=[wrong_review],
                rights_refs=[self.rights("confirmed")],
                policy_snapshot_refs=[self.policy_ref],
                release_id="rc001",
                override_reason="",
                override_risks=[],
                confirmed=True,
            )

    def test_non_audio_master_is_rejected_without_partial_release(self) -> None:
        bad_master = self.base / "bad.wav"
        bad_master.write_bytes(b"not audio")
        bad_review = self.approved_review(subject_sha256=sha256_file(bad_master))
        with self.assertRaisesRegex(RuntimeError, "Invalid data|ffprobe failed"):
            freeze_release(
                self.root,
                track_id="track-one",
                master=bad_master,
                title="Bad Master",
                actor=ACTOR,
                listening_gate_refs=[bad_review],
                rights_refs=[self.rights("confirmed")],
                policy_snapshot_refs=[self.policy_ref],
                release_id="rc001",
                override_reason="",
                override_risks=[],
                confirmed=True,
            )
        self.assertFalse((self.root / "releases" / "rc001").exists())

    def test_publication_urls_must_match_frozen_tag_and_asset(self) -> None:
        self.freeze([self.rights("confirmed")])
        digest = sha256_file(self.master)
        with self.assertRaisesRegex(ValueError, "tag does not match"):
            record_publication(
                self.root,
                release_id="rc001",
                actor=ACTOR,
                release_url="https://github.com/example/project/releases/tag/wrong",
                asset_url="https://github.com/example/project/releases/download/wrong/master.wav",
                asset_name="master.wav",
                asset_sha256=digest,
                confirmed=True,
            )
        with self.assertRaisesRegex(ValueError, "asset name"):
            record_publication(
                self.root,
                release_id="rc001",
                actor=ACTOR,
                release_url="https://github.com/example/project/releases/tag/release%2Frc001",
                asset_url="https://github.com/example/project/releases/download/release%2Frc001/other.wav",
                asset_name="master.wav",
                asset_sha256=digest,
                confirmed=True,
            )

    def test_release_must_freeze_the_exact_generation_policy_snapshot(self) -> None:
        register_generation(
            self.root,
            track_id="track-one",
            actor=ACTOR,
            provider="suno",
            operation="create",
            occurred_at="2026-08-10T10:00:00+08:00",
            model="fixture-model",
            object_id="policy-binding",
            plan="paid",
            prompt_ref="prompt:p001",
            lyrics_ref="lyrics:l001",
            terms_snapshot_ref=self.policy_ref,
            parent_refs=[],
            input_refs=[],
            output_refs=[],
            provider_data=[],
        )
        other_ref = "policies/platforms/suno/other.toml"
        atomic_write_toml(
            self.root / other_ref,
            {
                "snapshot_id": "suno-other",
                "provider": "suno",
                "retrieved_at": "2026-08-11",
                "snapshot_kind": "source-reference",
            },
        )
        with self.assertRaisesRegex(LedgerError, "exact generation policy snapshot"):
            freeze_release(
                self.root,
                track_id="track-one",
                master=self.master,
                title="Wrong Policy",
                actor=ACTOR,
                listening_gate_refs=[self.review_ref],
                rights_refs=[self.rights("confirmed")],
                policy_snapshot_refs=[other_ref],
                release_id="rc001",
                override_reason="",
                override_risks=[],
                confirmed=True,
            )

    def test_unrelated_release_staging_is_preserved(self) -> None:
        staging = self.root / "releases" / ".staging" / "rc001"
        staging.mkdir(parents=True)
        marker = staging / "keep.txt"
        marker.write_text("keep\n", encoding="utf-8")
        self.freeze([self.rights("confirmed")])
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")

    def test_release_gates_are_scoped_to_the_target_track(self) -> None:
        self.new_track("track-two")
        self.seal_texts("track-two")
        self.approved_review("track-two")
        wrong_review = self.approved_review("track-two")
        wrong_rights = self.rights("confirmed", "track-two")
        with self.assertRaisesRegex(LedgerError, "missing, ambiguous, or wrong type"):
            freeze_release(
                self.root,
                track_id="track-one",
                master=self.master,
                title="Wrong Gate",
                actor=ACTOR,
                listening_gate_refs=[wrong_review],
                rights_refs=[wrong_rights],
                policy_snapshot_refs=[self.policy_ref],
                release_id="rc001",
                override_reason="",
                override_risks=[],
                confirmed=True,
            )

    def test_freeze_rejects_symlinked_staging_root(self) -> None:
        outside = self.base / "outside-release"
        outside.mkdir()
        staging = self.root / "releases" / ".staging"
        staging.parent.mkdir(exist_ok=True)
        staging.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escapes repository"):
            self.freeze([self.rights("confirmed")])
        self.assertFalse((outside / "rc001" / self.master.name).exists())

    def test_freeze_rejects_tampered_or_nonhuman_gates(self) -> None:
        rights_ref = self.rights("confirmed")
        review_path = self.root / "tracks" / "track-one" / "reviews" / "lr001.toml"
        review = load_toml(review_path)
        review["decision"] = "selected"
        atomic_write_toml(review_path, review)
        with self.assertRaisesRegex(LedgerError, "minimal validation"):
            self.freeze([rights_ref])

        review["decision"] = "approved"
        atomic_write_toml(review_path, seal_record(review))
        rights_path = self.root / "tracks" / "track-one" / "rights" / "re001.toml"
        rights = load_toml(rights_path)
        rights["assessed_by"] = {"type": "agent", "id": "not-human"}
        atomic_write_toml(rights_path, seal_record(rights))
        with self.assertRaisesRegex(LedgerError, "assessed_by"):
            self.freeze([rights_ref])


if __name__ == "__main__":
    unittest.main()
