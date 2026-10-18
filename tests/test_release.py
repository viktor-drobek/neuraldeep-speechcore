"""Release metadata checks.

This file is shared by every neuraldeep-* repository and must stay identical across them.
"""

import os
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).parents[1]
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def skill_version():
    """Return `metadata.version` from SKILL.md, the key Coddy reads."""
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None
    head = text[4 : text.index("\n---\n", 4)]
    inside = False
    for line in head.splitlines():
        if not line.startswith(" "):
            inside = line == "metadata:"
        elif inside and line.startswith("  version:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return None


class ReleaseTests(unittest.TestCase):
    def test_skill_declares_a_semantic_version(self):
        version = skill_version()
        self.assertIsNotNone(version, "SKILL.md has no metadata.version")
        self.assertRegex(version, SEMVER)

    def test_release_tag_matches_the_skill_version(self):
        if os.environ.get("GITHUB_REF_TYPE") != "tag":
            self.skipTest("not a tag build")
        self.assertEqual(os.environ.get("GITHUB_REF_NAME"), f"v{skill_version()}")

    def test_agents_md_documents_the_release_steps(self):
        guide = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for needle in ("## Releases", "metadata.version", "gh release create"):
            self.assertIn(needle, guide)


if __name__ == "__main__":
    unittest.main()
