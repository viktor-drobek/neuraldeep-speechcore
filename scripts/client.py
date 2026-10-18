#!/usr/bin/env python3
"""Small stdlib client with a fail-closed Starter guard and resumable jobs."""
import argparse
import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

SERVICE = "speechcore"
HUB = "https://api.neuraldeep.ru/v1"
SPEECH = "https://speechcore.neuraldeep.ru/api"
PRICES = "https://neuraldeep.ru/api/public/wallet-prices"
QUOTAS = {"search": "/search/quota", "ocr": "/ocr/balance", "images": "/images/quota",
          "diarize": "/audio/diarize/quota"}
OPERATIONS = {"search": ("web", "tg", "crawl"), "ocr": ("extract",),
              "images": ("generate", "upscale", "background/remove", "enhance", "avatar"),
              "speechcore": ("upload",), "whisper": ("transcribe",), "diarize": ("diarize",)}
# Speech-to-text models on /v1/audio/transcriptions; each is its own public price ID.
TRANSCRIPTION_MODELS = ("gigaam-v3", "whisper-1", "whisper-podlodka-turbo")
DEFAULT_TRANSCRIPTION_MODEL = "gigaam-v3"
# Services that answer the submission directly, without a provider job to resume.
SYNCHRONOUS = {"search", "whisper", "diarize"}
# Provider limits for /v1/audio/diarize: up to 4 hours and 500 MB per file.
MAX_DIARIZE_MINUTES = 240
MAX_DIARIZE_BYTES = 500 * 1000 * 1000
# Field holding the provider job ID in each async submission response.
JOB_ID_FIELDS = {"images": "task_uid", "ocr": "id", "speechcore": "task_id"}
# Documented in SKILL.md; tests keep the two in sync.
POLL_INTERVAL = 2
DEFAULT_TIMEOUT = 120
# Explicit terminal failures. Any other unrecognized status keeps polling until the
# deadline: providers report intermediate states outside any fixed list.
FAILED_STATUSES = {"failed", "cancelled", "canceled", "error", "expired", "rejected"}


class Blocked(RuntimeError):
    pass


class RequestError(RuntimeError):
    pass


class JobError(RuntimeError):
    pass


class PollTimeout(JobError):
    pass


def resolve_key():
    home = Path(os.environ.get("CODDY_HOME") or Path.home() / ".coddy").expanduser()
    try:
        auth = json.loads((home / "providers/neuraldeep/neuraldeep-auth.json").read_text(encoding="utf-8"))
        key = auth.get("api_key") if isinstance(auth, dict) else None
    except (OSError, ValueError):
        key = None
    for value in (key, os.environ.get("NEURALDEEP_API_KEY")):
        if isinstance(value, str) and value.strip() and value.strip().lower() != "null":
            value = value.strip()
            if any(c.isspace() for c in value):
                continue
            return value
    raise Blocked("No credential; use coddy providers login neuraldeep or NEURALDEEP_API_KEY")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(method, url, key, body=None, content_type="application/json", *, timeout=30, raw=False, opener=None):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc not in
            {"api.neuraldeep.ru", "speechcore.neuraldeep.ru", "neuraldeep.ru"}):
        raise Blocked("Unapproved API origin")
    headers = {"Content-Type": content_type}
    if url != PRICES:
        headers["Authorization"] = "Bearer " + key
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with (opener or build_opener(NoRedirect())).open(req, timeout=timeout) as response:
            data = response.read()
            media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        return (data, media_type) if raw else json.loads(data)
    except HTTPError as error:
        error.close()
        raise RequestError(f"HTTP {error.code}; no automatic retry") from None
    except (URLError, TimeoutError, OSError, ValueError, HTTPException):
        raise RequestError("Transport or response error; no automatic retry") from None


def price_id(service, operation, model=None):
    prefix = "image" if service == "images" else service
    if service == "whisper":
        return model or DEFAULT_TRANSCRIPTION_MODEL
    if service == "speechcore":
        return "speechcore"
    if service == "diarize":
        return "audio:diarize"
    return prefix + ":" + operation.replace("/", "_")


def guard(service, operation, limits, prices, quota, units, model=None):
    try:
        if operation not in OPERATIONS[service]:
            raise Blocked("Unsupported operation")
        if service == "whisper":
            model = model or DEFAULT_TRANSCRIPTION_MODEL
            if model not in TRANSCRIPTION_MODELS:
                raise Blocked("Unsupported transcription model")
        if (limits["key"]["billing_mode"] != "subscription" or
                limits["key"]["status"] != "ok" or limits["tier"] != "starter"):
            raise Blocked("Requires an ok key with Starter subscription billing")
        rows = [p for p in prices["prices"] if p["model"] == price_id(service, operation, model)]
        if len(rows) != 1 or rows[0].get("premium") is not False:
            raise Blocked("Operation is not explicitly non-premium in public prices")
        if type(units) is not int or units <= 0:
            raise Blocked("Positive integer quota cost required")
        if service == "diarize" and units > MAX_DIARIZE_MINUTES:
            raise Blocked("Audio longer than 4 hours is not accepted")
        if service == "speechcore":
            raise Blocked("SpeechCore remaining-quota endpoint unverified; uploads disabled")
        if service == "whisper":
            raise Blocked(f"{model} remaining-quota endpoint unverified; transcription disabled")
        if quota["tier"] != "starter":
            raise Blocked("Service quota tier differs from Starter")
        if service == "search":
            bucket = quota["crawl" if operation == "crawl" else "search"]
            if bucket["mode"] != "tier":
                raise Blocked("Search quota is not a subscription tier bucket")
            remaining = [bucket[w]["remaining"] for w in ("day", "month")]
        elif service == "images":
            remaining = [quota["img"][w]["remaining"] for w in ("day", "month")]
        elif service == "diarize":
            # This bucket reports used and limit minutes, not a remaining count.
            if quota["unit"] != "minute":
                raise Blocked("Diarization quota is not counted in minutes")
            windows = [quota[w] for w in ("day", "month")]
            if any(type(b[f]) is not int or b[f] < 0 for b in windows for f in ("used", "limit")):
                raise Blocked("Insufficient or invalid service quota")
            remaining = [b["limit"] - b["used"] for b in windows]
        else:
            remaining = [quota[w]["remaining"] for w in ("daily_pages", "monthly_pages")]
        if any(type(n) is not int or n < units for n in remaining):
            raise Blocked("Insufficient or invalid service quota")
    except (KeyError, TypeError, AttributeError):
        raise Blocked("Unrecognized limits, prices, or service quota schema") from None


def preflight(service, operation, key, units, model=None):
    limits = request("GET", HUB + "/limits", key)
    prices = request("GET", PRICES, key)
    quota = request("GET", HUB + QUOTAS[service], key) if service in QUOTAS else {}
    guard(service, operation, limits, prices, quota, units, model=model)
    # Persist only relevant quota fields, never the account-wide limits response.
    fields = {"search": ("search", "crawl"), "ocr": ("daily_pages", "monthly_pages"), "images": ("img",),
              "diarize": ("unit", "day", "month")}
    return {"observed_at": now(), "endpoint": HUB + QUOTAS[service],
            "units_required": units, "buckets": {k: quota[k] for k in fields[service]}}


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_write(path, data):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".nd-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_state(path, state):
    atomic_write(path, json.dumps(state, indent=2).encode())


def state_template(task_id, service):
    return {"task_id": task_id, "service": service, "provider_job_id": None,
            "status": "blocked", "artifacts": [], "quota_observed": None,
            "timeouts": {}, "errors": [], "updated_at": now()}


def new_state(path, task_id, service):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Blocked("State file already exists; keep it for reconciliation and choose a new private state path") from None
    os.close(fd)
    state = state_template(task_id, service)
    save_state(path, state)
    return state


def save_artifact(path, data, endpoint):
    atomic_write(path, data)
    return {"reference": str(path), "sha256": hashlib.sha256(data).hexdigest(),
            "provenance": {"endpoint": endpoint, "retrieved_at": now(), "trust": "untrusted_data"}}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def check_segments(segments, label):
    """Raise ValueError unless every segment has a string `label` and 0 <= start <= end."""
    if not isinstance(segments, list):
        raise ValueError()
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get(label), str):
            raise ValueError()
        start, end = segment.get("start"), segment.get("end")
        if not finite(start) or not finite(end) or not 0 <= start <= end:
            raise ValueError()


def validate_diarization(value):
    """Raise ValueError unless value has the documented /v1/audio/diarize JSON shape."""
    if not isinstance(value, dict) or value.get("success") is False or value.get("error"):
        raise ValueError()
    if not finite(value.get("duration")) or value["duration"] < 0:
        raise ValueError()
    for field in ("num_speakers", "minutes_charged"):
        if type(value.get(field)) is not int or value[field] < 0:
            raise ValueError()
    check_segments(value.get("segments"), "speaker")
    return value


def validate_result(service, response):
    if not isinstance(response, tuple) or len(response) != 2:
        raise RequestError("Missing typed result response; resume the same provider job")
    data, media_type = response
    if not isinstance(data, bytes) or not data:
        raise RequestError("Empty or invalid result body; resume the same provider job")
    if service == "images":
        if media_type != "image/png" or len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RequestError("Result is not a PNG image; resume the same provider job")
        return data
    if media_type != "application/json":
        raise RequestError("Result is not JSON; resume the same provider job")
    try:
        value = json.loads(data)
        if not isinstance(value, dict) or value.get("success") is False or value.get("error"):
            raise ValueError()
        if service == "ocr":
            if not isinstance(value.get("content"), str):
                raise ValueError()
        elif service == "speechcore":
            if not finite(value.get("duration")) or value["duration"] < 0:
                raise ValueError()
            check_segments(value.get("segments"), "text")
        else:
            raise ValueError()
    except (ValueError, TypeError):
        raise RequestError("Invalid result schema; resume the same provider job") from None
    return data


def merge_transcript(diarization, transcript):
    """Label each timed transcript segment with the speaker it overlaps most.

    Ties go to the speaker who appeared first; a segment that overlaps no
    speaker keeps `speaker: null`. Consecutive segments of one speaker form a turn.
    """
    try:
        validate_diarization(diarization)
    except (ValueError, TypeError):
        raise Blocked("Diarization input does not match the /v1/audio/diarize JSON shape") from None
    try:
        if not isinstance(transcript, dict):
            raise ValueError()
        check_segments(transcript.get("segments"), "text")
    except (ValueError, TypeError):
        raise Blocked("Transcript is not a JSON object with timed text segments") from None
    speakers = list(dict.fromkeys(s["speaker"] for s in diarization["segments"]))
    turns, unassigned = [], 0
    for segment in transcript["segments"]:
        text = segment["text"].strip()
        if not text:
            continue
        start, end = segment["start"], segment["end"]
        overlap = dict.fromkeys(speakers, 0.0)
        for turn in diarization["segments"]:
            overlap[turn["speaker"]] += max(0.0, min(end, turn["end"]) - max(start, turn["start"]))
        best = max(speakers, key=lambda s: overlap[s], default=None)
        speaker = best if best is not None and overlap[best] > 0 else None
        unassigned += speaker is None
        if turns and turns[-1]["speaker"] == speaker:
            turns[-1]["end"] = max(turns[-1]["end"], end)
            turns[-1]["text"] += " " + text
        else:
            turns.append({"speaker": speaker, "start": start, "end": end, "text": text})
    return {"speakers": speakers, "turns": turns, "unassigned_segments": unassigned}


def merge_files(args):
    sources = [Path(args.diarization), Path(args.transcript)]
    if Path(args.output).resolve() in {p.resolve() for p in sources}:
        raise Blocked("Output must differ from the inputs")
    raw = [p.read_bytes() for p in sources]
    merged = merge_transcript(*(json.loads(data.decode("utf-8")) for data in raw))
    merged["sources"] = {name: hashlib.sha256(data).hexdigest() for name, data in zip(("diarization", "transcript"), raw)}
    data = json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")
    return {"status": "completed", "artifacts": [save_artifact(args.output, data, "local:merge")]}


def poll(get, status_url, result_url, success, *, timeout=DEFAULT_TIMEOUT, interval=POLL_INTERVAL):
    deadline = time.monotonic() + timeout
    last = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Name the last status only when it is a plain word; never echo provider text.
            seen = "" if last is None else f"; last status {last!r}" if re.fullmatch(r"[a-z_]{1,32}", last) else "; last status unrecognized"
            raise PollTimeout(f"Polling deadline reached{seen}; resume the same provider job")
        response = get(status_url, timeout=min(30, remaining))
        status = response.get("status") if isinstance(response, dict) else None
        if not isinstance(status, str):
            raise RequestError("Status response has no status; resume the same provider job")
        if time.monotonic() >= deadline:
            raise PollTimeout("Polling deadline reached; result not fetched")
        if status == success:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PollTimeout("Polling deadline reached; result not fetched")
            return get(result_url, timeout=min(30, remaining), raw=True)
        if status in FAILED_STATUSES:
            raise JobError(f"Provider job ended with status {status!r}")
        last = status
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def job_urls(service, job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
        raise JobError("Missing or invalid provider job ID; reconcile before submitting again")
    if service == "images":
        url = HUB + "/images/tasks/" + quote(job_id)
        return url, url + "/result", "finished"
    if service == "ocr":
        url = HUB + "/ocr/jobs/" + quote(job_id)
        return url, url + "/result?format=markdown", "completed"
    if service == "speechcore":
        url = SPEECH + "/transcriptions/" + quote(job_id)
        return url + "/status", url, "completed"
    raise JobError(f"Synchronous {service} has no resumable job endpoint")


def multipart(path, field, fields):
    boundary = uuid.uuid4().hex
    # Local names may contain quotes or line breaks; never copy them into headers.
    suffix = Path(path).suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", Path(path).suffix) else ""
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="upload{suffix}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
    parts.extend([Path(path).read_bytes(), f"\r\n--{boundary}--\r\n".encode()])
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def submission(args):
    if SERVICE == "speechcore":
        raise Blocked("SpeechCore uploads disabled until remaining quota is verified")
    if SERVICE == "whisper":
        raise Blocked(f"{getattr(args, 'model', None) or DEFAULT_TRANSCRIPTION_MODEL} transcription disabled until remaining quota is verified")
    payload = json.loads(Path(args.payload).read_text(encoding="utf-8")) if args.payload else {}
    if not isinstance(payload, dict):
        raise Blocked("Payload must be a JSON object")
    url = HUB + "/audio/diarize" if SERVICE == "diarize" else HUB + "/" + SERVICE + "/" + args.operation
    if SERVICE == "search":
        if args.operation == "tg":
            return "GET", url + "?" + urlencode(payload), None, "application/json"
        return "POST", url, json.dumps(payload).encode(), "application/json"
    if SERVICE == "images" and args.operation == "generate":
        return "POST", url, json.dumps(payload).encode(), "application/json"
    if not args.file:
        raise Blocked("A local --file is required")
    if SERVICE == "diarize" and Path(args.file).stat().st_size > MAX_DIARIZE_BYTES:
        raise Blocked("Audio file is larger than 500 MB")
    fields = {"model_profile": args.profile} if SERVICE == "ocr" else {}
    if payload:
        raise Blocked("Extra multipart fields are not supported by this minimal helper")
    body, content_type = multipart(args.file, "file" if SERVICE in ("ocr", "diarize") else "image", fields)
    return "POST", url, body, content_type


def execute(args):
    if args.command == "merge":
        return merge_files(args)
    key = resolve_key()
    units = 1
    if SERVICE == "ocr":
        units = args.pages * (2 if args.profile == "pro" else 1)
    elif SERVICE == "diarize":
        units = getattr(args, "minutes", 0)
    if args.command == "check":
        return {"status": "ready", "quota_observed": preflight(SERVICE, args.operation, key, units, model=getattr(args, "model", None))}
    if Path(args.state).resolve() == Path(args.output).resolve():
        raise Blocked("State and artifact paths must differ")
    if args.command == "resume":
        if SERVICE in SYNCHRONOUS:
            raise Blocked(f"Synchronous {SERVICE} has no resumable job endpoint")
        if args.job_id:
            job_urls(SERVICE, args.job_id)
            state = new_state(args.state, args.task_id, SERVICE)
            state["provider_job_id"] = args.job_id
            state["status"] = "submitted"
            save_state(args.state, state)
        else:
            state = json.loads(Path(args.state).read_text(encoding="utf-8"))
        # A hand-edited or corrupted state must block, not crash or poll.
        if (not isinstance(state, dict) or not isinstance(state.get("status"), str)
                or not isinstance(state.get("errors"), list)):
            raise Blocked("State is not resumable; reconcile without submitting again")
        if state["service"] != SERVICE or state["status"] not in {"submitted", "timed_out", "poll_error"}:
            raise Blocked("State is not resumable; reconcile without submitting again")
    else:
        state = new_state(args.state, args.task_id, SERVICE)
    state["timeouts"] = {"request_seconds": 30, "poll_seconds": args.timeout}
    try:
        if args.command == "run":
            method, url, body, content_type = submission(args)
            state["quota_observed"] = preflight(SERVICE, args.operation, key, units, model=getattr(args, "model", None))
            # An interruption from here until an ID is persisted requires reconciliation.
            state["status"] = "reconciliation_required"
            save_state(args.state, state)
            response = request(method, url, key, body, content_type)
            if SERVICE == "search":
                if not isinstance(response, dict) or response.get("success") is False or response.get("error"):
                    raise JobError("Search returned an error response")
                expected = "pages" if args.operation == "crawl" else "results"
                if not isinstance(response.get(expected), list):
                    raise JobError("Unrecognized search response")
                data = json.dumps(response, ensure_ascii=False, indent=2).encode()
                result_url = url.split("?")[0]
            elif SERVICE == "diarize":
                try:
                    validate_diarization(response)
                except (ValueError, TypeError):
                    raise JobError("Unrecognized diarization response") from None
                data = json.dumps(response, ensure_ascii=False, indent=2).encode()
                result_url = url
            else:
                state["provider_job_id"] = response.get(JOB_ID_FIELDS[SERVICE])
                job_urls(SERVICE, state["provider_job_id"])
                state["status"] = "submitted"
                save_state(args.state, state)
        if SERVICE not in SYNCHRONOUS:
            status_url, result_url, success = job_urls(SERVICE, state["provider_job_id"])
            data = poll(lambda url, **kw: request("GET", url, key, **kw),
                        status_url, result_url, success, timeout=args.timeout)
            data = validate_result(SERVICE, data)
        state["artifacts"] = [save_artifact(args.output, data, result_url)]
        state["status"] = "completed"
    except Blocked as error:
        state["status"] = "blocked"
        state["errors"].append(str(error))
    except PollTimeout as error:
        state["status"] = "timed_out"
        state["errors"].append(str(error))
    except (RequestError, JobError, OSError, ValueError, AttributeError) as error:
        if state["status"] not in {"reconciliation_required", "blocked"}:
            state["status"] = "failed" if isinstance(error, JobError) else "poll_error"
        state["errors"].append(str(error) if isinstance(error, (RequestError, JobError)) else "Local I/O or response schema error")
    state["updated_at"] = now()
    save_state(args.state, state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "run", "resume", "merge"))
    parser.add_argument("operation", nargs="?", choices=OPERATIONS[SERVICE], default=OPERATIONS[SERVICE][0])
    parser.add_argument("--payload", help="JSON request file (search or image generation)")
    parser.add_argument("--file", help="Input file for OCR, image processing or diarization")
    parser.add_argument("--pages", type=int, default=0, help="Verified OCR input page count; required before upload")
    parser.add_argument("--profile", choices=("fast", "pro"), default="fast")
    parser.add_argument("--model", choices=TRANSCRIPTION_MODELS, help="Transcription model (default gigaam-v3)")
    parser.add_argument("--minutes", type=int, default=0, help="Verified audio duration in whole minutes, rounded up; required before diarization")
    parser.add_argument("--diarization", help="merge: diarization JSON from /v1/audio/diarize")
    parser.add_argument("--transcript", help="merge: transcript JSON with timed text segments")
    parser.add_argument("--state", default="job-state.json", help="Private durable state; must not exist for a new submission")
    parser.add_argument("--output", default="result.bin")
    parser.add_argument("--task-id", default="standalone")
    parser.add_argument("--job-id", help="Attach an existing provider job for read-only resume")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Polling deadline in seconds")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    if args.model and SERVICE != "whisper":
        parser.error("--model applies only to transcription")
    if (args.minutes or args.command == "merge") and SERVICE != "diarize":
        parser.error("--minutes and merge apply only to diarization")
    if args.command == "merge" and not (args.diarization and args.transcript):
        parser.error("merge needs --diarization and --transcript")
    if args.command != "merge" and (args.diarization or args.transcript):
        parser.error("--diarization and --transcript apply only to merge")
    try:
        result = execute(args)
    except (Blocked, RequestError, JobError) as error:
        result = {"status": "blocked", "errors": [str(error)]}
    except (OSError, ValueError, KeyError):
        result = {"status": "blocked", "errors": ["Cannot proceed; check credentials, quota and private state. Do not resubmit an uncertain job."]}
    result = {**state_template(args.task_id, SERVICE), **result}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"completed", "ready"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
