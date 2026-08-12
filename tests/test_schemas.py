from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CanonicalRecordSchemaTests(unittest.TestCase):
    def test_evidence_events_require_submission_and_output_bindings(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/core-record.schema.json").read_text(encoding="utf-8")
        )
        event_rule = schema["allOf"][0]
        self.assertEqual(
            set(event_rule["if"]["properties"]["record_type"]["enum"]),
            {
                "generation",
                "edit",
                "export",
                "render",
                "review",
                "rights_evidence",
                "release_candidate",
            },
        )
        self.assertEqual(
            set(event_rule["then"]["required"]),
            {
                "submission_id",
                "envelope_digest",
                "submitted_by",
                "output_binding",
                "seal",
            },
        )

    def test_human_responsibility_fields_are_conditional(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/core-record.schema.json").read_text(encoding="utf-8")
        )
        rules = {
            rule["if"]["properties"]["record_type"]["const"]:
            rule["then"]["required"]
            for rule in schema["allOf"][1:]
        }
        self.assertEqual(rules["review"], ["reviewed_by"])
        self.assertEqual(rules["rights_evidence"], ["assessed_by"])
        self.assertEqual(rules["release_candidate"], ["confirmed_by"])


if __name__ == "__main__":
    unittest.main()
