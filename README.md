# 🎙️ neuraldeep-speechcore

Transcription of long audio/video (up to ~6 hours) with diarization, speaker labels, hotwords, and export to SRT/VTT/DOCX/PDF.

A [Coddy](https://coddy.dev) / AI-agent skill for the **NeuralDeep SpeechCore API**.

## Author

These skills were authored by the **kimi2.6** and **qwen3.8 27b** models, and are published by **viktor-drobek**.

## Install

```bash
# via the skillsbd catalog
npx skillsbd add viktor-drobek/neuraldeep-speechcore/neuraldeep-speechcore

# or directly from this repo
npx skillsbd add https://github.com/viktor-drobek/neuraldeep-speechcore --skill neuraldeep-speechcore
```

## Requirements

- A NeuralDeep API key (`sk-...`). The skill reads it from
  `${CODDY_HOME:-~/.coddy}/providers/neuraldeep/neuraldeep-auth.json` (field `api_key`),
  falling back to `NEURALDEEP_API_KEY` when the file/key is missing, null, empty, or malformed.
- Python 3.10+ (standard library only).
- Get a key at <https://neuraldeep.ru> (Hub) or via `coddy providers login neuraldeep`.

## Usage

See [`SKILL.md`](./SKILL.md) for the full API reference, endpoints, request/response
shapes, and worked examples.

## Starter guard and Relay handoff

See [STARTER_RELAY.md](./STARTER_RELAY.md) for guarded commands, private job
state, timeout recovery, and the optional artifact-based Relay envelope.
The helper checks live service quota rather than hard-coding daily limits.
SpeechCore upload is blocked until a remaining-quota endpoint is verified;
resuming existing jobs remains read-only.

Run offline tests: `python3 -B -m unittest discover -s tests`.

## License

[MIT](./LICENSE)
