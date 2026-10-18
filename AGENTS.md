# Agent instructions

Instructions for coding agents (Codex, Coddy and others) working **on** this
repository. The skill itself, which agents load to transcribe audio, is described
in [SKILL.md](SKILL.md); its safety rules are in [STARTER_RELAY.md](STARTER_RELAY.md).

## Project

- `SKILL.md`: the skill and NeuralDeep SpeechCore API reference.
- `scripts/client.py`: a standard-library helper with a fail-closed Starter guard
  and resumable jobs. SpeechCore uploads are **blocked on purpose**: only
  `check upload` (always `blocked`) and `resume` of an existing job work.
- `tests/`: offline tests with mocked HTTP. `TESTING.md` records every change and
  the current test count.
- Python 3.10+ standard library only. No dependencies, no build step.

## Setup and tests

No install is needed. Run the tests from the repository root:

```bash
python3 -B -m unittest discover -s tests
```

Before opening a PR, also run these. All must pass, the warning count must be 0:

```bash
LC_ALL=C PYTHONUTF8=0 PYTHONCOERCECLOCALE=0 python3 -B -m unittest discover -s tests
python3 -X warn_default_encoding -B -m unittest discover -s tests 2>&1 | grep -c EncodingWarning
python3 -B scripts/client.py --help
git diff --check
```

CI (`.github/workflows/tests.yml`) runs the suite and `--help` on Python 3.10 and
3.x with no credential. `-B` keeps `__pycache__` out of the tree.

## Rules

- **Never call billed or live endpoints.** Do not run `scripts/client.py run`.
  `check` and `resume` use the user's real API key; run them only when the user
  asks. Tests must mock HTTP and must not need a credential.
- **Fail closed.** No retries or resubmission of a billed POST, no redirects, no
  API key or provider response body in errors or output. An ambiguous submission
  is `reconciliation_required`, not a reason to try again.
- **Do not unblock SpeechCore or whisper-1 uploads.** They stay blocked until a
  remaining-quota endpoint is verified; that is the user's decision.
- **Test first.** Add or change a test, show it fails, then fix. Record the change
  and the new test count in `TESTING.md`.
- **Keep the shared helper identical.** `scripts/client.py` is shared with
  neuraldeep-search, -images, -ocr, -transcriptions and -diarization; only the `SERVICE = "..."` line
  differs. Port any helper change to the other repos, and check with:

  ```bash
  diff <(grep -v '^SERVICE = ' scripts/client.py) <(grep -v '^SERVICE = ' ../neuraldeep-search/scripts/client.py)
  ```

- **UTF-8 everywhere.** Pass `encoding="utf-8"` to every `read_text()`,
  `write_text()`, `open()` in text mode and `subprocess.run(text=True)`, in code,
  tests and Python examples in the docs. The docs tests check the examples.
- **Docs examples must stay safe.** Fenced `python` and `bash` blocks in the
  `.md` files are parsed (`ast.parse`, `bash -n`), never executed. Do not add
  examples that upload, spend quota or print the key.
- **Private runtime data stays out of git.** State files, transcripts, inputs,
  keys and `.env` files are private. `.gitignore` covers the example names; do not
  commit others either.

## Credentials

The helper reads this skill's own `NEURALDEEP_SPEECHCORE_API_KEY`, then the shared
`NEURALDEEP_API_KEY`, each from the environment, then from
`${CODDY_HOME:-~/.coddy}/.env` (several keys separated by commas; the first that
passes the free checks is used). It does not read the Coddy provider file. Never
print, log or commit a key; ask the user to add a missing one to `~/.coddy/.env`,
never in the chat.

## Delivery workflow

Work RED-GREEN-REFACTOR: start a feature with a test that describes the observable result and a
bug fix with a regression test, watch it fail for the intended reason, make the smallest change
that passes, and refactor only with the suite green. Then run the full offline matrix above.

Inside `scripts/client.py`, implement from lower dependencies upward: constants and parsing,
credentials and approved-origin transport, the fail-closed guard, durable state and artifacts,
provider jobs and polling, then `execute()` and the CLI. Lower layers must not depend on CLI
parsing or provider job state.

Update `README.md`, `SKILL.md`, `STARTER_RELAY.md` and `TESTING.md` in the same change when
behavior, CLI output, safety assumptions or documented shapes change.

## Rules Sync

This file is the common policy for every agent, and `CLAUDE.md` is a symlink to it. Scoped detail
lives in native rules with identical bodies:

| Rule | Scope | Cursor | Claude Code |
|---|---|---|---|
| `workflow` | always on | `.cursor/rules/workflow.mdc` | `.claude/rules/workflow.md` |
| `client` | `scripts/**/*.py`, `tests/test_client.py` | `.cursor/rules/client.mdc` | `.claude/rules/client.md` |
| `documentation` | `README.md`, `SKILL.md`, `STARTER_RELAY.md`, `TESTING.md`, `tests/test_examples.py` | `.cursor/rules/documentation.mdc` | `.claude/rules/documentation.md` |

- Cursor reads `.cursor/rules/`; Claude Code reads `CLAUDE.md` and `.claude/rules/`.
- Codex reads this file, and the project hook in `.codex/hooks.json` with
  `.codex/hooks/attach_rules.py` attaches the Cursor rules: always-on rules at session start,
  scoped rules before an edit to a matching file. Run `/hooks` in Codex once per clone and again
  after changing either hook file. The hook is a fail-open convenience, not a security boundary;
  the safety rules here, the helper and the tests still enforce them.
- Never copy rule bodies under `.codex` or create `.codex/rules.md`.
- Change a rule and its counterpart together. `tests/test_agent_rules.py` checks the pairing, the
  scopes and the hook; it and `.codex/hooks/` are identical across the neuraldeep-* repositories.

## Releases

A release is a `vX.Y.Z` tag on `main` with a GitHub release. `metadata.version` in the `SKILL.md`
frontmatter is the version Coddy shows and uses to decide whether an installed copy is older, so
it must equal the tag without the `v`:

1. In a PR, bump `metadata.version` (semantic versioning) and note the change in `TESTING.md`.
2. After the PR is merged and CI is green, tag the merge commit and publish the release, for
   example `gh release create v1.0.0 --target main --title v1.0.0 --notes-file NOTES.md`.
3. CI runs on the tag push; `tests/test_release.py` fails the build if the tag and
   `metadata.version` differ. It is identical across the neuraldeep-* repositories.

## Git and PRs

- Work on a branch and open a PR against `main`; do not push to `main`. Merging is
  the owner's decision.
- Do not force-push shared branches or rewrite published history without the
  owner's explicit approval.
- Do not add co-author trailers or "Generated with ..." lines for AI agents to
  commits or PR descriptions.
- Several agent sessions may work in these repos at once. Check `git status` and
  fetch before you start, and do not change a checkout or branch another session
  is using.

## Agent notes

- **Codex** reads this file automatically. The tests need no network and write
  only to temporary directories, so the default sandbox should be enough; ask before running `check` or `resume`, which need
  network and the user's key.
- **Coddy** loads this file as project instructions. Use ask or plan mode for
  reviews. The skill under development is `SKILL.md`; do not install it from
  this checkout into `${CODDY_HOME:-~/.coddy}` unless the user asks.
