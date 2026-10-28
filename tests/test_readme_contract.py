"""Shared README contract for every self-contained NeuralDeep skill."""

import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).parents[1]
SECTIONS = (
    "Author", "Install", "Requirements", "Usage",
    "Starter guard and Relay handoff", "Agent rules", "Tests", "License",
)


def literal_assignment(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"Missing literal {name} in {path.name}")


class ReadmeContractTests(unittest.TestCase):
    def setUp(self):
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.skill = ROOT.name

    def test_required_image_template_sections_are_present_and_ordered(self):
        headings = re.findall(r"^## (.+)$", self.readme, re.M)
        positions = []
        for section in SECTIONS:
            self.assertIn(section, headings)
            self.assertEqual(headings.count(section), 1, section)
            positions.append(headings.index(section))
        self.assertEqual(positions, sorted(positions))

    def test_author_and_installation_identify_this_skill(self):
        self.assertRegex(self.readme, r"(?m)^## Author\n\n\*\*Viktor Drobek\*\*\n")
        self.assertIn(f"https://github.com/viktor-drobek/{self.skill}", self.readme)
        self.assertIn(f"--skill {self.skill}", self.readme)
        self.assertIn("Python 3.10+", self.readme)
        self.assertIn("standard library", self.readme)

    def test_usage_describes_operations_inputs_and_outputs(self):
        service = literal_assignment(ROOT / "scripts" / "client.py", "SERVICE")
        api = ROOT / "scripts" / "service_api.py"
        if api.exists():
            operations = literal_assignment(api, "SERVICES")[service]
        else:
            operations = literal_assignment(ROOT / "scripts" / "client.py", "OPERATIONS")[service]
        for operation in operations:
            with self.subTest(operation=operation):
                self.assertRegex(self.readme, rf"(?<![a-zA-Z0-9_]){re.escape(operation)}(?![a-zA-Z0-9_])")
        for needle in ("scripts/client.py", "--help", "--state", "--output"):
            self.assertIn(needle, self.readme)
        self.assertRegex(self.readme.lower(), r"input|payload|recording|document")
        self.assertRegex(self.readme.lower(), r"output|artifact|result")
        self.assertIn("|", self.readme, "Describe capabilities and status, not only a link to SKILL.md")

    def test_guard_and_private_runtime_boundaries_are_explicit(self):
        for needle in ("blocked", "reconciliation_required", "outside", "checkout", "untrusted"):
            self.assertIn(needle, self.readme)
        self.assertIn("STARTER_RELAY.md", self.readme)
        self.assertNotIn("curl https://api.neuraldeep.ru", self.readme)
        self.assertNotIn("curl https://drift.neuraldeep.ru", self.readme)

    def test_recovery_guide_has_no_stale_wallet_workflow_claims(self):
        guide = (ROOT / "STARTER_RELAY.md").read_text(encoding="utf-8")
        for obsolete in (
            "this helper only resumes existing SpeechCore",
            "transcription never reports `ready`",
            "Unblocking it needs a verified remaining-minutes bucket",
        ):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, guide)

    def test_agent_mechanisms_and_limits_are_documented(self):
        for needle in (
            "Codex", "Claude Code", "Cursor", "AGENTS.md", "CLAUDE.md",
            ".cursor/rules/", ".claude/rules/", ".codex/hooks.json", "/hooks",
            "alwaysApply", "globs", "paths", "trust", "fail-open", "security boundary",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.readme)
        self.assertIn("clone", self.readme)
        self.assertIn("unrelated project", self.readme)

    def test_documentation_links_and_offline_tests_are_available(self):
        for name in ("SKILL.md", "STARTER_RELAY.md", "TESTING.md", "AGENTS.md", "LICENSE"):
            self.assertTrue((ROOT / name).is_file())
            self.assertIn(name, self.readme)
        self.assertIn("unittest discover -s tests", self.readme)
        self.assertIn("CODDY_HOME=/nonexistent", self.readme)
        self.assertIn("git diff --check", self.readme)


if __name__ == "__main__":
    unittest.main()
