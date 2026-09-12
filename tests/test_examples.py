import ast
from pathlib import Path
import re
import subprocess
import unittest


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


if __name__ == "__main__":
    unittest.main()
