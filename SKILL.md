---
author: kimi2.6 & qwen3.8 27b (published by viktor-drobek)
name: neuraldeep-speechcore
description: >
  Transcription skill powered by NeuralDeep SpeechCore API.
  Transcribes long audio/video (up to ~6 hours), supports diarization, speaker labels,
  hotwords, custom prompts, and export to SRT/VTT/DOCX/PDF.
  Automatically activates when user asks for transcription, STT, audio-to-text,
  meeting analysis, or podcast processing.
metadata:
  neuraldeep:
    emoji: "🎙️"
    requires_file: "~/.coddy/providers/neuraldeep/neuraldeep-auth.json"
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

## Requirements

### Primary key source
Read from the Coddy provider auth file:
```bash
~/.coddy/providers/neuraldeep/neuraldeep-auth.json
```
That JSON contains `api_key` (e.g. `sk-...`).

Extract inline:
```bash
jq -r '.api_key' ~/.coddy/providers/neuraldeep/neuraldeep-auth.json
```

### Fallback key source (env)
```bash
${NEURALDEEP_API_KEY}
```

### System requirement
- The `curl` tool is available.
- The `jq` tool is available (or fallback to Python/Node).
- Audio/video file is accessible on local filesystem.

## Resolve API key helper

```bash
ND_KEY=$(jq -r '.api_key' ~/.coddy/providers/neuraldeep/neuraldeep-auth.json 2>/dev/null)
[ -z "$ND_KEY" ] && ND_KEY="${NEURALDEEP_API_KEY}"
```
If both are empty → return `BLOCKED` and suggest: `coddy providers login neuraldeep`.

## Base URL

```
https://speechcore.neuraldeep.ru/api
```

---

## 1. Upload and Start Transcription

### curl example
```bash
ND_KEY=$(jq -r '.api_key' ~/.coddy/providers/neuraldeep/neuraldeep-auth.json 2>/dev/null || echo "${NEURALDEEP_API_KEY}")
curl -X POST \
  "https://speechcore.neuraldeep.ru/api/upload?diarize=true&diarize_speakers_num=3&language=ru&hotwords=NeuralDeep,Kimi,RAG&initial_prompt=Технический%20созвон%20про%20LLM" \
  -H "Authorization: Bearer ${ND_KEY}" \
  -F "file=@meeting.mp3"
```

**Options (URL query parameters):**
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
ND_KEY=$(jq -r '.api_key' ~/.coddy/providers/neuraldeep/neuraldeep-auth.json 2>/dev/null || echo "${NEURALDEEP_API_KEY}")
TID="<task_id_from_upload>"
curl -sS "https://speechcore.neuraldeep.ru/api/transcriptions/${TID}/status" \
  -H "Authorization: Bearer ${ND_KEY}"
```

**Response:**
```json
{"status": "processing", "progress": 45}
# or:
{"status": "completed", "progress": 100}
```

**Polling interval:** check every 2–5 seconds. Keep a reasonable timeout (upload to ~6h recordings may take a while).

---

## 3. Fetch Transcript Result

### Full JSON with segments (text + timestamps + speakers)
```bash
ND_KEY=$(jq -r '.api_key' ~/.coddy/providers/neuraldeep/neuraldeep-auth.json 2>/dev/null || echo "${NEURALDEEP_API_KEY}")
TID="<task_id>"
curl -sS "https://speechcore.neuraldeep.ru/api/transcriptions/${TID}" \
  -H "Authorization: Bearer ${ND_KEY}"
```

**Response fields:**
- `detected_language`
- `duration`
- `segments` — array of `{speaker, text, start, end}` objects

### Markdown with timestamps
```bash
curl -sS "https://speechcore.neuraldeep.ru/api/transcriptions/${TID}/markdown" \
  -H "Authorization: Bearer ${ND_KEY}"
```

### List your transcriptions
```bash
curl -sS "https://speechcore.neuraldeep.ru/api/transcriptions?limit=20" \
  -H "Authorization: Bearer ${ND_KEY}"
```

---

## 4. Complete Python Example

```python
import time, httpx, json, os

BASE = "https://speechcore.neuraldeep.ru/api"
KEY = json.load(open(os.path.expanduser(
    "~/.coddy/providers/neuraldeep/neuraldeep-auth.json"
)))["api_key"]
H = {"Authorization": f"Bearer {KEY}"}

# 1. Upload
params = {
    "diarize": "true",
    "diarize_speakers_num": 3,
    "language": "ru",
    "hotwords": "NeuralDeep, Kimi, RAG",
    "initial_prompt": "Технический созвон про LLM",
}
with open("meeting.mp3", "rb") as f:
    tid = httpx.post(f"{BASE}/upload", headers=H, params=params, files={"file": f}).json()["task_id"]

# 2. Poll (every 2 seconds, max 600 attempts ~20 min)
for i in range(600):
    st = httpx.get(f"{BASE}/transcriptions/{tid}/status", headers=H).json()
    if st["status"] in ("completed", "failed"):
        break
    time.sleep(2)

# 3. Fetch data = httpx.get(f"{BASE}/transcriptions/{tid}", headers=H).json()
print("lang:", data["detected_language"], "duration:", data["duration"])
print(httpx.get(f"{BASE}/transcriptions/{tid}/markdown", headers=H).text)
```

---

## Workflow Summary

1. **Resolve key** (file or env)
2. **POST /upload** with file + options → get `task_id`
3. **Poll /status** until `completed` or `failed`
4. **GET /transcriptions/{id}** (JSON) or `.../markdown` (human-readable)

## Cost & Limits

- Counted in **transcriptions per day** (1/day free; starter 50/day; pro 200/day).
- Only the `POST /upload` call consumes a transcription credit.
- Status polling and result fetching do **not** consume credits.

## Error Handling

- `401 Unauthorized` — invalid key → re-run `coddy providers login neuraldeep`
- `429` — daily limit reached → include `Retry-After` header
- Large files: keep a generous polling timeout; queue processing is GPU-backed.
- If `failed`, retry with the same options or contact support with `task_id`.

## Privacy & Security

- Do not log `task_id` publicly unless necessary.
- Audio is processed on NeuralDeep infrastructure.
- Do not upload files containing personal data of third parties without consent.
