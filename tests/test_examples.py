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
        for name in ("README.md", "SKILL.md", "STARTER_RELAY.md"):
            text = (root / name).read_text()
            for number, match in enumerate(re.finditer(r"```(python|bash)\n(.*?)```", text, re.S)):
                with self.subTest(file=name, block=number):
                    if match[1] == "python":
                        ast.parse(match[2])
                    else:
                        subprocess.run(["bash", "-n"], input=match[2], text=True, check=True, capture_output=True)
            self.assertNotIn("ND_KEY=$(jq", text)
            self.assertNotIn("# 3. Fetch data =", text)

    def test_documented_polling_defaults_match_helper(self):
        text = (Path(__file__).parents[1] / "SKILL.md").read_text()
        interval = re.search(r"\*\*Polling interval:\*\* the helper checks status every (\d+) seconds", text)
        timeout = re.search(r"`--timeout` defaults to (\d+) seconds", text)
        self.assertIsNotNone(interval)
        self.assertIsNotNone(timeout)
        self.assertEqual(inspect.signature(client.poll).parameters["interval"].default, int(interval[1]))
        with patch.object(client, "execute", return_value={"status": "ready"}) as execute, patch.object(sys, "argv", ["client.py", "check"]), patch("sys.stdout"):
            client.main()
        self.assertEqual(execute.call_args.args[0].timeout, int(timeout[1]))


if __name__ == "__main__":
    unittest.main()
