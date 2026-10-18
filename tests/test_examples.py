import ast
import importlib.util
import inspect
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("client", Path(__file__).parents[1] / "scripts" / "client.py")
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)


class ExampleTests(unittest.TestCase):
    def test_fenced_examples_parse_without_execution(self):
        root = Path(__file__).parents[1]
        for name in ("AGENTS.md", "README.md", "SKILL.md", "STARTER_RELAY.md"):
            text = (root / name).read_text(encoding="utf-8")
            for number, match in enumerate(re.finditer(r"```(python|bash)\n(.*?)```", text, re.S)):
                with self.subTest(file=name, block=number):
                    if match[1] == "python":
                        ast.parse(match[2])
                    else:
                        subprocess.run(["bash", "-n"], input=match[2], text=True, check=True, capture_output=True, encoding="utf-8")
            self.assertNotIn("ND_KEY=$(jq", text)
            self.assertNotIn("# 3. Fetch data =", text)

    def test_documented_polling_defaults_match_helper(self):
        text = (Path(__file__).parents[1] / "SKILL.md").read_text(encoding="utf-8")
        interval = re.search(r"\*\*Polling interval:\*\* the helper checks status every (\d+) seconds", text)
        timeout = re.search(r"`--timeout` defaults to (\d+) seconds", text)
        self.assertIsNotNone(interval)
        self.assertIsNotNone(timeout)
        self.assertEqual(inspect.signature(client.poll).parameters["interval"].default, int(interval[1]))
        with patch.object(client, "execute", return_value={"status": "ready"}) as execute, patch.object(sys, "argv", ["client.py", "check"]), patch("sys.stdout"):
            client.main()
        self.assertEqual(execute.call_args.args[0].timeout, int(timeout[1]))

    def test_python_examples_use_explicit_text_encoding(self):
        # Copied examples must not depend on the locale encoding either.
        root = Path(__file__).parents[1]
        for name in ("AGENTS.md", "README.md", "SKILL.md", "STARTER_RELAY.md"):
            text = (root / name).read_text(encoding="utf-8")
            for number, match in enumerate(re.finditer(r"```python\n(.*?)```", text, re.S)):
                for node in ast.walk(ast.parse(match[1])):
                    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                        continue
                    keywords = {k.arg for k in node.keywords}
                    text_mode = node.func.attr in ("read_text", "write_text") or (
                        node.func.attr == "run" and "text" in keywords)
                    with self.subTest(file=name, block=number, call=node.func.attr, line=node.lineno):
                        if text_mode:
                            self.assertIn("encoding", keywords)

    def test_skill_says_blocked_is_final_and_forbids_bypass(self):
        # On 2026-10-17 a live agent read the credential file, uploaded audio
        # directly and hand-wrote a state file to resume it.
        skill = (Path(__file__).parents[1] / "SKILL.md").read_text(encoding="utf-8")
        section = skill.split("## When the helper says `blocked`", 1)
        self.assertEqual(len(section), 2, "missing blocked section")
        head, rules = section[0], section[1].split("\n## ", 1)[0]
        self.assertLess(len(head.split("\n# ", 1)[1]), 1500, "blocked section must come early")
        for needle in ("final answer", "${CODDY_HOME:-~/.coddy}/.env", "NEURALDEEP_SPEECHCORE_API_KEY", "NEURALDEEP_API_KEY",
                       "POST /upload", "by hand", "the user gave you", "supervisor", "undocumented"):
            self.assertIn(needle, rules)
        self.assertIn("never bypass", skill.split("---", 2)[1])


if __name__ == "__main__":
    unittest.main()
