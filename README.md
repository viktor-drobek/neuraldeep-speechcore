# 🎙️ neuraldeep-speechcore

Upload a recording with a wallet key after the user approves the cost, resume jobs, and save validated transcript JSON with timestamps and speaker labels.
A subscription key cannot upload until a remaining-quota endpoint is verified; SRT/VTT/DOCX/PDF exports are produced locally from the saved segments.

A [Coddy](https://coddy.dev) / AI-agent skill for the **NeuralDeep SpeechCore API**.

## Author

**Viktor Drobek**

## Install

```bash
# via the skillsbd catalog
npx skillsbd add viktor-drobek/neuraldeep-speechcore/neuraldeep-speechcore

# or directly from this repo
npx skillsbd add https://github.com/viktor-drobek/neuraldeep-speechcore --skill neuraldeep-speechcore
```

## Requirements

- A NeuralDeep API key (`sk-...`) in this skill's own variable `NEURALDEEP_SPEECHCORE_API_KEY` (several
  keys may be separated by commas), or the shared `NEURALDEEP_API_KEY`, set in the
  environment or in `${CODDY_HOME:-~/.coddy}/.env`:

  ```bash
  echo 'NEURALDEEP_SPEECHCORE_API_KEY=sk-...' >> ~/.coddy/.env
  ```
- Python 3.10+ (standard library only).
- Get a key at <https://neuraldeep.ru> (Hub).

## Usage

See [`SKILL.md`](./SKILL.md) for the full API reference, endpoints, request/response
shapes, and worked examples.

## Starter guard and Relay handoff

See [STARTER_RELAY.md](./STARTER_RELAY.md) for guarded commands, private job
state, timeout recovery, and the optional artifact-based Relay envelope.
The helper checks live service quota rather than hard-coding daily limits.
A subscription upload is blocked until a remaining-quota endpoint is verified; a
wallet key uploads only up to the amount the user approved (`--wallet-limit`), and
resuming an existing job is read-only.

Run offline tests: `python3 -B -m unittest discover -s tests`.

## License

[MIT](./LICENSE)
