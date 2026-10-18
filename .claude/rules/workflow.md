---
description: Test-first delivery, verification and Rules Sync
---

# Delivery workflow

This always-on rule supplements the common policy in `AGENTS.md`.

## RED-GREEN-REFACTOR

1. Start every feature with a focused test that describes the observable result.
2. Start every bug fix with a regression test that reproduces the defect.
3. Run that test and confirm it fails for the intended reason.
4. Make the smallest change that turns it green.
5. Refactor only while the focused and full suites stay green.

Run the full offline matrix before reporting completion:

```bash
env -u NEURALDEEP_API_KEY CODDY_HOME=/nonexistent python3 -B -m unittest discover -s tests
env -u NEURALDEEP_API_KEY CODDY_HOME=/nonexistent LC_ALL=C PYTHONUTF8=0 python3 -B -m unittest discover -s tests
env -u NEURALDEEP_API_KEY CODDY_HOME=/nonexistent python3 -X warn_default_encoding -B -m unittest discover -s tests
python3 -B scripts/client.py --help
```

Never run `run` or `resume` against the live API while developing or testing. Keep keys, state
files, payloads and result artifacts outside the checkout. State exactly which checks ran and
whether they passed.

## Rules Sync

`AGENTS.md` is the common policy and `CLAUDE.md` is a symlink to it. Each rule exists twice with
an identical body: `.cursor/rules/<name>.mdc` for Cursor and `.claude/rules/<name>.md` for Claude
Code. An always-on rule has `alwaysApply: true` in Cursor and no `paths` in Claude Code; a scoped
rule has `alwaysApply: false` with `globs` in Cursor and the same list as `paths` in Claude Code.
The Codex hook in `.codex/` reads `.cursor/rules/*.mdc` directly, so never copy rule bodies under
`.codex` or create `.codex/rules.md`.

When a rule or `AGENTS.md` changes, update every counterpart in the same change and run
`tests/test_agent_rules.py`, which checks the pairing, the scopes and the hook. That test and
`.codex/hooks/` are identical across the neuraldeep-* repositories; port a change to each sibling.
