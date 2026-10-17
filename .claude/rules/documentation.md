---
description: Skill documentation and its tests
paths:
  - "README.md"
  - "SKILL.md"
  - "STARTER_RELAY.md"
  - "TESTING.md"
  - "tests/test_examples.py"
---

# Documentation

Keep `README.md`, `SKILL.md`, `STARTER_RELAY.md`, `TESTING.md` and the documentation tests
consistent with what the helper actually does.

- Document provider behavior only when a source, a recorded read-only observation or a test
  supports it. Never advertise an operation the helper refuses.
- Examples are parsed, never executed: `bash` blocks must pass `bash -n` and `python` blocks
  must parse with `ast.parse`. Never add an example that reads the key or calls the API directly.
- For each test-first change, add a short note to `TESTING.md` naming the test that failed first,
  and update the documented test count.

This rule is scoped to documentation. The safety policy in `AGENTS.md` applies to every file.
