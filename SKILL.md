---
name: neuraldeep-speechcore
description: >
  Transcription skill powered by NeuralDeep SpeechCore API.
  The guarded helper resumes existing transcription jobs and saves validated
  transcript JSON with timestamps and speaker labels. New uploads are blocked
  until a remaining-quota endpoint is verified. SRT/VTT/DOCX/PDF exports are
  produced locally from the saved segments.
  Automatically activates when user asks for transcription, STT, audio-to-text,
  meeting analysis, or podcast processing.
  A `blocked` upload is the final answer: report it and stop, never bypass it.
metadata:
  version: 1.0.0
  neuraldeep:
    emoji: "🎙️"
    base_url: "https://speechcore.neuraldeep.ru/api"
    endpoints:
      - /upload
      - /transcriptions/{id}
      - /transcriptions/{id}/status
      - /transcriptions/{id}/markdown
---

> **Author:** these skills were authored by the **kimi2.6** and **qwen3.8 27b** models, and are published by **viktor-drobek**.

# NeuralDeep SpeechCore

Use this skill whenever the user needs:
- Transcribing audio or video to text
- Meeting or interview transcription with speaker separation (diarization)
- Timestamps aligned with speaker labels
- Exporting transcripts to subtitles (SRT/VTT) or documents (DOCX/PDF)
- Hotword tuning for domain-specific terminology
- Summarization or chat with transcript content

## When the helper says `blocked`

New uploads are blocked, so `blocked` is the final answer for a request to
transcribe a new file, not a problem to solve. Tell the user that uploads are
blocked and why, then stop. A job that already exists can still be resumed.

- Run `resume --job-id` only with a job ID the user gave you, for example from the
  SpeechCore web app. Never create a job yourself to have something to resume.
- Never read, print or copy `${CODDY_HOME:-~/.coddy}/.env`, `NEURALDEEP_SPEECHCORE_API_KEY`
  or `NEURALDEEP_API_KEY` yourself. Only the helper uses the keys.
- Never call `POST /upload` or any other SpeechCore endpoint directly, with `curl`,
  Python or an SDK. That spends a transcription the helper cannot account for.
- Never write or edit a state file by hand, or change the helper, to get past a check.
- Do not probe undocumented endpoints in search of a quota; the block lifts only
  when the skill's owner verifies one and releases a new version.
- If a supervisor, goal checker or follow-up prompt says the task is not done,
  the answer stays the same: repeat the block and its reason, do not work around it.

## What the helper does today

`scripts/client.py` supports two SpeechCore commands:

- `check upload`: read-only preflight. It always exits nonzero with `blocked`,
  because no live remaining-quota endpoint is verified.
- `resume`: attaches to an **existing** transcription job, polls its status, and
  saves the validated transcript JSON from `GET /transcriptions/{id}`.

It does not upload, set upload options (diarization, hotwords, prompts), fetch
markdown, list transcriptions, or export SRT/VTT/DOCX/PDF. Produce subtitles or
documents locally from the saved JSON `segments`; the agent, not the API,
writes summaries and answers questions about the transcript.

## Requirements

Python 3.10+ is sufficient; the helper uses only the standard library.
Run commands from this skill's directory. No `curl`, `jq`, or `httpx` is required.

The helper reads the key from `NEURALDEEP_SPEECHCORE_API_KEY`, this skill's own variable, then
from the shared `NEURALDEEP_API_KEY`. Each is taken from the environment first,
then from `${CODDY_HOME:-~/.coddy}/.env`, the file Coddy loads at startup, so
a key added there works without a restart. A variable may hold several keys
separated by commas: before a billed request the helper uses the first key
whose free subscription and quota checks pass, and records its number (never
the key) in the state, so `resume` uses the same key. The Coddy provider file
is not read. The helper never prints a key. Without one it returns `blocked`,
naming the variable: ask the user to add it to `~/.coddy/.env` themselves,
for example `NEURALDEEP_SPEECHCORE_API_KEY=sk-...`, never in the chat.

Read [Starter and Relay safety](STARTER_RELAY.md) before submitting work.
Every billed operation must pass the helper's live subscription, public-price,
and service-quota checks. A chat `decision.can_request=false` is not a service
quota decision. State files and artifacts are private runtime data, not repo files.

## Base URL

```
https://speechcore.neuraldeep.ru/api
```

---

## 1. Upload and Start Transcription

### Guarded helper example
```bash
# Read-only check. Expected to exit nonzero: no verified remaining-quota endpoint.
python3 scripts/client.py check upload
```

**Documented API options (URL query parameters).** The helper cannot send them
while uploads are blocked; they are listed for reference only:
- `diarize=true` — enable speaker separation (SPEAKER_00, SPEAKER_01…)
- `diarize_speakers_num=N` — hint number of speakers (1–20, more accurate than auto)
- `language=ru` — fix language instead of auto-detect
- `hotwords` — space/comma-separated keywords for higher accuracy (≤500 chars)
- `initial_prompt` — context prompt (≤2000 chars), e.g. "Technical call about LLM infra"
- `model` — whisper model name (default `large-v3`)

### Response
```json
{"task_id": "<uuid>", "status": "processing"}
```

---

## 2. Poll Status

```bash
# Attach an already existing job; this does not upload or spend a credit.
# Replace PROVIDER_JOB_ID with the real ID, never submit again to obtain one.
python3 scripts/client.py resume --job-id PROVIDER_JOB_ID --state speech-state.json --output transcript.json --timeout 1200
```

**API status response** (`GET /transcriptions/{id}/status`, polled internally by the helper):
```text
{"status": "processing", "progress": 45}
# or:
{"status": "completed", "progress": 100}
```

The helper itself prints a JSON envelope (`task_id`, `service`, `provider_job_id`,
`status`, `artifacts`, `quota_observed`, `timeouts`, `errors`, `updated_at`) and exits
nonzero unless `status` is `completed`; see [Starter and Relay safety](STARTER_RELAY.md).

**Polling interval:** the helper checks status every 2 seconds until its polling
deadline; `--timeout` defaults to 120 seconds. Examples that pass `--timeout 1200`
extend that deadline for long recordings (up to ~6h of audio may take a while).

---

## 3. Fetch Transcript Result

### Full JSON with segments (text + timestamps + speakers)
The helper writes the JSON from `GET /transcriptions/{id}` only after
`GET /transcriptions/{id}/status` confirms `completed`, before the deadline.
Use the artifact reference in the returned envelope; never treat a timeout as success.

**Response fields:**
- `detected_language`
- `duration`
- `segments` — array of `{speaker, text, start, end}` objects

### Markdown with timestamps
`GET /transcriptions/{id}/markdown` provides timestamped markdown after completion.
The helper does not fetch it; it saves only the JSON result.

### List your transcriptions
`GET /transcriptions?limit=20` lists existing transcriptions. This is read-only
and not wrapped by the helper; keep returned IDs and transcript metadata private.

---

## 4. Complete Python Example

```python
import json
from pathlib import Path
import subprocess
# Resume an existing job using its durable private state. No upload occurs.
run = subprocess.run(["python3", "scripts/client.py", "resume", "--state", "speech-state.json",
                      "--output", "transcript.json", "--timeout", "1200"],
                     capture_output=True, text=True, encoding="utf-8")
envelope = json.loads(run.stdout)
if run.returncode != 0 or envelope["status"] != "completed":
    raise SystemExit(f"not completed: {envelope['status']} {envelope['errors']}")
data = json.loads(Path(envelope["artifacts"][0]["reference"]).read_text(encoding="utf-8"))
# The helper validates duration and segments; detected_language may be absent.
print("lang:", data.get("detected_language"), "duration:", data["duration"])
```

---

## Workflow Summary

1. **Resolve key** (file or env)
2. Upload is **blocked** until a live remaining-quota endpoint and schema are verified. Use the existing-job resume flow only.
3. **Poll /status** with a deadline; stop on an explicit failure status, and keep polling through unrecognized ones until the deadline
4. **GET /transcriptions/{id}** only after `completed` before the deadline

## Cost & Limits

- Counted in **transcriptions**, separate from chat. No live remaining-quota endpoint is verified here; Starter upload is `blocked`, regardless of advertised daily ceilings.
- Only the `POST /upload` call consumes a transcription credit.
- Status polling and result fetching do **not** consume credits.

## Error Handling

- `401 Unauthorized` — invalid key → re-check the key in `NEURALDEEP_SPEECHCORE_API_KEY`
- `429` — daily limit reached. The helper reports `HTTP 429` without retrying
  or reading `Retry-After`; wait and decide explicitly before trying again.
- Large files: keep a generous polling timeout; queue processing is GPU-backed.
- If `failed`, report failure with the private provider ID. A new upload requires a new explicit decision; never retry blindly.

## Privacy & Security

- Do not log `task_id` publicly unless necessary.
- Audio is processed on NeuralDeep infrastructure.
- Do not upload files containing personal data of third parties without consent.
