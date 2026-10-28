"""Contract tests for the agent rule surfaces (AGENTS.md, Cursor, Claude Code, Codex).

This file is shared by every neuraldeep-* repository and must stay identical across them.
"""

import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
CURSOR = ROOT / ".cursor" / "rules"
CLAUDE = ROOT / ".claude" / "rules"
CODEX = ROOT / ".codex"
HOOK = CODEX / "hooks" / "attach_rules.py"


def load_hook():
    spec = importlib.util.spec_from_file_location("attach_rules", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def split_frontmatter(text, required=True):
    if not text.startswith("---\n"):
        if not required:
            return "", text.strip()
        raise AssertionError("rule has no frontmatter")
    end = text.index("\n---\n", 4)
    return text[4:end], text[end + 5 :].strip()


def claude_paths(head):
    paths, inside = [], False
    for line in head.splitlines():
        if line.startswith("paths:"):
            inside = True
        elif inside and line.startswith("  - "):
            paths.append(line[4:].strip().strip('"'))
        elif inside:
            inside = False
    return paths


def repository_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for name in filenames:
            out.append((Path(dirpath) / name).relative_to(ROOT).as_posix())
    return out


class AgentRuleTests(unittest.TestCase):
    def setUp(self):
        self.hook = load_hook()
        self.cursor = {p.stem: p for p in sorted(CURSOR.glob("*.mdc"))}
        self.claude = {p.stem: p for p in sorted(CLAUDE.glob("*.md"))}

    def test_claude_md_is_a_symlink_to_agents_md(self):
        claude_md = ROOT / "CLAUDE.md"
        self.assertTrue(claude_md.is_symlink())
        self.assertEqual(os.readlink(claude_md), "AGENTS.md")

    def test_agents_md_carries_the_common_workflow_and_rules_sync(self):
        guide = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for needle in (
            "## Delivery workflow",
            "RED-GREEN-REFACTOR",
            "## Rules Sync",
            ".cursor/rules/",
            ".claude/rules/",
            ".codex/hooks.json",
            "/hooks",
        ):
            self.assertIn(needle, guide)

    def test_cursor_and_claude_rules_are_paired_with_identical_bodies(self):
        self.assertIn("workflow", self.cursor)
        self.assertEqual(sorted(self.cursor), sorted(self.claude))
        for name in self.cursor:
            with self.subTest(rule=name):
                _, cursor_body = split_frontmatter(self.cursor[name].read_text(encoding="utf-8"))
                _, claude_body = split_frontmatter(self.claude[name].read_text(encoding="utf-8"), required=False)
                self.assertTrue(cursor_body)
                self.assertEqual(cursor_body, claude_body)
        _, workflow = split_frontmatter(self.cursor["workflow"].read_text(encoding="utf-8"))
        for needle in ("RED-GREEN-REFACTOR", "## Rules Sync"):
            self.assertIn(needle, workflow)

    def test_rule_scopes_map_between_hosts(self):
        for name in self.cursor:
            with self.subTest(rule=name):
                rule = self.hook.parse_rule(self.cursor[name])
                head, _ = split_frontmatter(self.claude[name].read_text(encoding="utf-8"), required=False)
                self.assertTrue(rule.description)
                if name == "workflow":
                    self.assertTrue(rule.always)
                    self.assertEqual(rule.globs, [])
                    self.assertNotIn("paths:", head)
                else:
                    self.assertFalse(rule.always, "a scoped rule must not be always-on")
                    self.assertTrue(rule.globs)
                    self.assertEqual(rule.globs, claude_paths(head))

    def test_every_scoped_glob_matches_a_repository_file(self):
        files = repository_files()
        for name in self.cursor:
            for pattern in self.hook.parse_rule(self.cursor[name]).globs:
                with self.subTest(rule=name, glob=pattern):
                    regex = self.hook.glob_to_regex(pattern)
                    self.assertTrue(any(regex.match(path) for path in files))

    def test_rule_files_are_ascii(self):
        for path in [*self.cursor.values(), *self.claude.values()]:
            with self.subTest(rule=path.name):
                path.read_bytes().decode("ascii")

    def test_codex_hook_is_wired_without_a_rule_copy(self):
        config = json.loads((CODEX / "hooks.json").read_text(encoding="utf-8"))
        for event in ("SessionStart", "PreToolUse"):
            with self.subTest(event=event):
                commands = [h["command"] for group in config["hooks"][event] for h in group["hooks"]]
                self.assertTrue(any(".codex/hooks/attach_rules.py" in c for c in commands))
        self.assertFalse((CODEX / "rules.md").exists())
        self.assertEqual([p for p in CODEX.rglob("*") if p.suffix in (".md", ".mdc")], [])

    def run_hook(self, tmp, payload):
        env = dict(os.environ, TMPDIR=tmp, PYTHONIOENCODING="utf-8")
        data = payload if isinstance(payload, str) else json.dumps(payload)
        result = subprocess.run(
            [sys.executable, "-B", str(HOOK)],
            input=data,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout:
            return ""
        return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_hook_attaches_always_on_rules_at_session_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = self.run_hook(tmp, {"hook_event_name": "SessionStart", "session_id": "s", "source": "startup"})
        self.assertIn("<!-- from .cursor/rules/workflow.mdc -->", context)
        for name in self.cursor:
            if name != "workflow":
                self.assertNotIn(f".cursor/rules/{name}.mdc", context)

    def test_hook_attaches_scoped_rules_once_per_session_for_edits(self):
        def pre_tool(session, tool, tool_input):
            return {"hook_event_name": "PreToolUse", "session_id": session, "tool_name": tool, "tool_input": tool_input}

        cases = {
            "update": ("apply_patch", {"command": "*** Begin Patch\n*** Update File: scripts/client.py\n*** End Patch\n"}),
            "add": ("apply_patch", {"command": "*** Begin Patch\n*** Add File: scripts/extra.py\n*** End Patch\n"}),
            "delete": ("apply_patch", {"command": "*** Begin Patch\n*** Delete File: scripts/client.py\n*** End Patch\n"}),
            "rename": ("apply_patch", {"command": "*** Begin Patch\n*** Update File: LICENSE\n*** Move to: scripts/moved.py\n*** End Patch\n"}),
            "edit": ("Edit", {"file_path": str(ROOT / "scripts" / "client.py")}),
            "write": ("Write", {"file_path": "scripts/new.py"}),
        }
        for case, (tool, tool_input) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                first = self.run_hook(tmp, pre_tool(case, tool, tool_input))
                self.assertIn("<!-- from .cursor/rules/client.mdc -->", first)
                self.assertNotIn("workflow.mdc", first)
                self.assertEqual(self.run_hook(tmp, pre_tool(case, tool, tool_input)), "")

        with tempfile.TemporaryDirectory() as tmp:
            docs = self.run_hook(tmp, pre_tool("docs", "Edit", {"file_path": "SKILL.md"}))
            self.assertIn("<!-- from .cursor/rules/documentation.mdc -->", docs)
            self.assertNotIn("client.mdc", docs)
            self.assertEqual(self.run_hook(tmp, pre_tool("other", "Write", {"file_path": "LICENSE"})), "")

    def test_session_start_matcher_includes_fork(self):
        config = json.loads((CODEX / "hooks.json").read_text(encoding="utf-8"))
        for source in ("startup", "resume", "clear", "compact", "fork"):
            with self.subTest(source=source):
                self.assertTrue(any(
                    re.search(group.get("matcher", ".*"), source)
                    for group in config["hooks"]["SessionStart"]
                ), f"SessionStart does not match {source}")

    def test_fork_resets_scoped_rule_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            start = {"hook_event_name": "SessionStart", "session_id": "fork-probe", "source": "startup"}
            edit = {"hook_event_name": "PreToolUse", "session_id": "fork-probe", "tool_name": "Edit", "tool_input": {"file_path": "README.md"}}
            self.assertIn("workflow.mdc", self.run_hook(tmp, start))
            self.assertIn("documentation.mdc", self.run_hook(tmp, edit))
            self.assertEqual(self.run_hook(tmp, edit), "")
            self.assertIn("workflow.mdc", self.run_hook(tmp, dict(start, source="fork")))
            self.assertIn("documentation.mdc", self.run_hook(tmp, edit))

    def test_cursor_globs_accept_scalar_flow_and_block_lists(self):
        expected = ["scripts/**/*.py", "tests/*.py"]
        declarations = (
            'globs: scripts/**/*.py, tests/*.py',
            'globs: ["scripts/**/*.py", "tests/*.py"]',
            'globs:\n  - "scripts/**/*.py"\n  - "tests/*.py"',
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.mdc"
            for declaration in declarations:
                with self.subTest(declaration=declaration):
                    path.write_text('---\ndescription: Probe\nalwaysApply: false\n' + declaration + '\n---\n# Probe\n', encoding="utf-8")
                    self.assertEqual(self.hook.parse_rule(path).globs, expected)

    def test_hook_fails_open_on_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            for payload in ("not json", "{}", json.dumps({"hook_event_name": "PreToolUse", "tool_input": 7})):
                with self.subTest(payload=payload):
                    self.assertEqual(self.run_hook(tmp, payload), "")


if __name__ == "__main__":
    unittest.main()
