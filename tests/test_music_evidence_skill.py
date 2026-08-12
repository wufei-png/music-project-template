from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents/skills/music-evidence/SKILL.md"
OPENAI = ROOT / ".agents/skills/music-evidence/agents/openai.yaml"


class MusicEvidenceSkillTests(unittest.TestCase):
    def test_skill_has_complete_generic_frontmatter(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(match)
        frontmatter = match.group(1) if match else ""
        self.assertIn("name: music-evidence", frontmatter)
        self.assertIn("description:", frontmatter)
        self.assertNotIn("TODO", text)
        self.assertNotRegex(text, r"\b(?:Codex|Claude)\b")

    def test_skill_preserves_ledger_boundaries(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        normalized = " ".join(text.split()).lower()
        for required in (
            "explicit confirmation",
            "exactly one event",
            "same submission id and exact envelope",
            "release plan receipt",
            "pass_with_override",
            "never call it rights-cleared",
            "do not create a parallel receipt file",
            "do not search broadly",
            "do not initialize one",
            "may be outside the ledger",
            "never invoke publication commands within this skill",
        ):
            self.assertIn(required, normalized)
        self.assertNotIn("publication commands unless the user asks", normalized)

    def test_openai_metadata_names_the_skill_in_default_prompt(self) -> None:
        text = OPENAI.read_text(encoding="utf-8")
        self.assertIn('display_name: "Music Evidence"', text)
        self.assertIn("$music-evidence", text)
        self.assertNotIn("TODO", text)


if __name__ == "__main__":
    unittest.main()
