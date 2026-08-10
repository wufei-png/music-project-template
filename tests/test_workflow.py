from __future__ import annotations

import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.music_project.assets import safe_import
from scripts.music_project.cli import _hash_text, initialize_project, main as cli_main
from scripts.music_project.io import atomic_write_toml, load_toml, sha256_file
from scripts.music_project.records import (
    _next_id,
    new_track,
    record_review,
    record_rights,
    register_generation,
    seal_record,
)
from scripts.music_project.release import freeze_release, record_publication
from scripts.music_project.retention import apply_plan, build_plan, effective_policy
from scripts.music_project.validation import validate


ACTOR = "fixture-user"


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

    def approved_review(self, track_id: str = "track-one") -> str:
        path = record_review(
            self.root,
            track_id=track_id,
            actor=ACTOR,
            subject_ref=f"track:{track_id}",
            decision="approved",
            blind_label="A",
            timestamp_notes="full listen complete",
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
        self.assertEqual(validate(self.root, "minimal")["status"], "PASS")
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
        self.assertEqual(validate(self.root, "minimal")["status"], "PASS")


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
        report = validate(self.root, "minimal")
        self.assertIn("missing_ref", {issue["code"] for issue in report["errors"]})

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
        register_generation(
            self.root,
            track_id="track-one",
            actor=ACTOR,
            provider="suno",
            operation="create",
            occurred_at="2026-08-10T10:00:00+08:00",
            model="fixture-model",
            object_id="fixture-suno",
            plan="paid",
            prompt_ref="not-a-reference",
            lyrics_ref="lyrics:l001",
            terms_snapshot_ref="policies/platforms/suno/does-not-exist.toml",
            parent_refs=[],
            input_refs=[],
            output_refs=[],
            provider_data=[],
        )
        report = validate(self.root, "post")
        codes = {issue["code"] for issue in report["errors"]}
        self.assertIn("malformed_ref", codes)
        self.assertIn("terms_snapshot", codes)

    def test_confirmed_rights_require_evidence_locator_and_hash(self) -> None:
        self.new_track()
        with self.assertRaisesRegex(ValueError, "private-locator"):
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
        plan = build_plan(self.root, "track-one")
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            apply_plan(self.root, plan, ACTOR, confirmed=True)
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


class ReleaseTests(ProjectCase):
    def setUp(self) -> None:
        super().setUp()
        self.new_track()
        self.seal_texts()
        self.review_ref = self.approved_review()
        self.master = self.base / "master.wav"
        self.master.write_bytes(b"RIFF-final-master")
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

    def test_incomplete_rights_need_structured_override(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit override"):
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
        self.assertIn("frozen hash mismatch", messages)

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
        with self.assertRaisesRegex(ValueError, "non-overridable"):
            self.freeze(
                [evidence],
                override_reason="attempted override",
                override_risks=["known issue"],
            )

    def test_release_gates_are_scoped_to_the_target_track(self) -> None:
        self.new_track("track-two")
        self.seal_texts("track-two")
        self.approved_review("track-two")
        wrong_review = self.approved_review("track-two")
        wrong_rights = self.rights("confirmed", "track-two")
        with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
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
        with self.assertRaisesRegex(ValueError, "invalid seal"):
            self.freeze([rights_ref])

        review["decision"] = "approved"
        atomic_write_toml(review_path, seal_record(review))
        rights_path = self.root / "tracks" / "track-one" / "rights" / "re001.toml"
        rights = load_toml(rights_path)
        rights["human_confirmed"] = False
        atomic_write_toml(rights_path, seal_record(rights))
        with self.assertRaisesRegex(ValueError, "not human-confirmed"):
            self.freeze([rights_ref])


if __name__ == "__main__":
    unittest.main()
