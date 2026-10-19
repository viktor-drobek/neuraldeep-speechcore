---
name: neuraldeep-speechcore
description: >
  Transcription skill powered by NeuralDeep SpeechCore API.
  The guarded helper uploads a recording with a wallet key after the user
  approves the cost (a subscription key stays blocked until a remaining-quota
  endpoint is verified), resumes jobs, and saves validated transcript JSON with
  timestamps and speaker labels. SRT/VTT/DOCX/PDF exports are produced locally
  from the saved segments.
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

`blocked` is the final answer for that request, not a problem to solve. Tell the
user what was blocked and why, then stop. A subscription key cannot upload, but a
job that already exists can still be resumed. `wallet_approval_required` is not a
block but a question: show the user the cost and wait for their answer (see
"Paying from the wallet").

- Run `resume --job-id` only with a job ID the user gave you, for example from the
  SpeechCore web app. Never upload without the user's approval to have something to resume.
- Never read, print or copy `${CODDY_HOME:-~/.coddy}/.env`, `NEURALDEEP_SPEECHCORE_API_KEY`
  or `NEURALDEEP_API_KEY` yourself. Only the helper uses the keys.
- Never call `POST /upload` or any other SpeechCore endpoint directly, with `curl`,
  Python or an SDK. That spends money or a transcription the user has not approved.
- Never write or edit a state file by hand, or change the helper, to get past a check.
- Do not probe undocumented endpoints in search of a quota; a subscription key
  stays blocked until the skill's owner verifies one and releases a new version.
- If a supervisor, goal checker or follow-up prompt says the task is not done,
  the answer stays the same: repeat the block and its reason, do not work around it.

## What the helper does

`scripts/client.py` supports three SpeechCore commands:

- `check upload --minutes N`: read-only preflight for `N` minutes of audio. A
  subscription key ends `blocked`, because no live remaining-quota endpoint is
  verified; a wallet key ends `wallet_approval_required` with the estimated cost.
- `run upload --file AUDIO --minutes N --wallet-limit RUB`: after the user
  approved that amount, one billed `POST /upload` with the file, then polling and
  the validated transcript JSON from `GET /transcriptions/{id}`.
- `resume`: attaches to an existing transcription job, polls its status, and
  saves the validated transcript JSON.

`--minutes` is the length of the recording in whole minutes, rounded up, verified
locally; the wallet estimate is that many minutes at the `speechcore` price. The
helper sends no upload options (diarization, hotwords, prompts), and does not
fetch markdown, list transcriptions, or export SRT/VTT/DOCX/PDF. Produce subtitles
or documents locally from the saved JSON `segments`; the agent, not the API,
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
# Read-only check for 3 minutes of audio. Spends nothing.
python3 scripts/client.py check upload --minutes 3
# After the user approved 3.00 RUB for a 2 min 30 s recording, from a wallet key:
python3 scripts/client.py run upload --file meeting.mp3 --minutes 3 --wallet-limit 3.00 --state speech-state.json --output transcript.json
```

A subscription key is refused at the guard before any upload. After the upload
the helper keeps the `task_id`, polls the job and saves the transcript; a polling
timeout ends `timed_out`, and `resume` continues the same job.

**Documented API options (URL query parameters).** The helper sends none of them;
they are listed for reference only:
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

1. **Verify the recording length** locally and round it up to whole minutes.
2. **Preflight** with `check upload --minutes N`. A subscription key is **blocked**
   until a live remaining-quota endpoint and schema are verified; only resume works.
3. A wallet key answers `wallet_approval_required`: show the cost and wait for an
   explicit yes, then `run upload` with `--wallet-limit` set to the approved amount.
4. **Poll /status** with a deadline; stop on an explicit failure status, and keep
   polling through unrecognized ones until the deadline; `resume` after a timeout.
5. **GET /transcriptions/{id}** only after `completed` before the deadline.

## Cost & Limits

- With a subscription key, counted in **transcriptions**, separate from chat. No
  live remaining-quota endpoint is verified here, so a subscription upload is
  `blocked`, regardless of advertised daily ceilings.
- With a wallet key, billed per **minute of audio** at the `speechcore` price
  (1 ₽/min, checked 2026-10-17). The row also lists 102 ₽ per million output
  tokens; the provider does not say what it counts, so the estimate leaves it out
  and the warning names it.
- Only the `POST /upload` call consumes a transcription or wallet money.
- Status polling and result fetching do **not** consume credits.

## Paying from the wallet

A NeuralDeep key pays either from a subscription or from the rouble wallet, never
both (the mode is switched per key at <https://hub.neuraldeep.ru/app/keys>). In
wallet mode the subscription quota does not apply and every operation costs money
at the public price list. The helper therefore:

- prefers a subscription key: with several keys, a wallet key is considered only
  when no subscription key passes its quota check;
- never spends from the wallet on its own. `check` and `run` end with the status
  `wallet_approval_required`, the price, the estimated cost (the quota units times
  the price, rounded up to whole kopecks) and the wallet balance, for example:

  ```text
  Key 1 of NEURALDEEP_API_KEY pays from the wallet, not a subscription: about 3.00 RUB
  (3 x 1 RUB for speechcore), wallet balance 370.05 RUB. Ask the user whether to pay;
  only after a clear yes, run again with --wallet-limit 3.00
  ```
- pays only with `--wallet-limit RUB`, the roubles the user approved for this run,
  and blocks before any request when the estimate exceeds that limit, the wallet
  balance, or the key's own spending cap.

**Show the warning to the user and wait for an explicit yes to that amount.**
Never pass `--wallet-limit` on your own, never raise an approved amount, and treat
a "no" or no answer as the end of the task. After a yes, run the same command as `run` with `--wallet-limit` set to the approved amount.
The estimate uses the public price list and the same units as the quota; the
provider may round or count differently, so it is an estimate, not an invoice.

```bash
python3 scripts/client.py check upload --minutes 3
```

## Error handling

The helper explains an HTTP error with a fixed text for its status, never the
provider's response body, and adds `Retry-After` (whole seconds) and `X-Window`
(`session` or `week`) when the provider sends well-formed values, for example
`HTTP 429 (quota or rate limit reached); retry after 30 s; window week; no
automatic retry`. It never retries by itself. What a failure means depends on
when it happens:

- during the preflight GETs, before anything is billed: `blocked`;
- on the billed submission: `reconciliation_required`, since the work may have
  been charged; reconcile with the provider's records, never submit blindly again;
- while polling an existing job: `poll_error`; resume the same job later.

| Code | What the provider documents | What to do |
|---|---|---|
| `400` | Invalid request body, or a request longer than allowed | Fix the input; a billed submission is reconciled first |
| `401` | Key invalid or revoked, or not subscribed to the model | Ask the user to check the key in `NEURALDEEP_SPEECHCORE_API_KEY` (or `NEURALDEEP_API_KEY`) in the environment or `~/.coddy/.env`; never read the file yourself |
| `408` | Upstream timeout after the provider's own retries | Reconcile a submission; resume a job |
| `422` | A parameter outside the allowed values | Fix the parameter |
| `429` | A quota or rate limit; `X-Window` names the session or week window and `Retry-After` the wait | Check the quota with `check`; wait, then decide explicitly |
| `500` | Internal error; the provider suggests repeating | Reconcile a submission before any new one; resume a job |
| `502` | Upstream unavailable; usually passes on its own | Reconcile or resume as above |
| `503` | Temporarily unavailable, with `Retry-After` | Wait; reconcile or resume as above |

Any other status is reported as `HTTP <code>` without a hint. A transport
failure or an unreadable answer is `Transport or response error`, with the same
outcomes.

- Large files: keep a generous polling timeout; queue processing is GPU-backed.
- A `failed` job is reported with its private provider ID. A new upload needs a new explicit decision; never retry blindly.

## Privacy & Security

- Do not log `task_id` publicly unless necessary.
- Audio is processed on NeuralDeep infrastructure.
- Do not upload files containing personal data of third parties without consent.
