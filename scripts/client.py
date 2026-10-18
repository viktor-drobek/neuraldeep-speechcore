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
# The diarization answer arrives on the POST itself, so a long file needs a long
# read timeout: a cut-off answer may already be charged and must not be resent.
REQUEST_TIMEOUT = 30


def diarize_timeout(minutes):
    return max(120, 30 * minutes)

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


def request(method, url, key, body=None, content_type="application/json", *, timeout=REQUEST_TIMEOUT, raw=False, opener=None):
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
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


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


def label_segments(diarization, transcript):
    """Label each timed transcript segment with the speaker it overlaps most.

    Both inputs are ordered by time. Ties go to the speaker who spoke first; a
    segment that overlaps no speaker keeps `speaker: None`; empty text is skipped.
    Returns the speakers in order of first appearance and the labeled segments.
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
    timed = sorted(diarization["segments"], key=lambda s: (s["start"], s["end"]))
    speakers = list(dict.fromkeys(s["speaker"] for s in timed))
    labeled = []
    for segment in sorted(transcript["segments"], key=lambda s: (s["start"], s["end"])):
        text = segment["text"].strip()
        if not text:
            continue
        start, end = segment["start"], segment["end"]
        overlap = dict.fromkeys(speakers, 0.0)
        for turn in timed:
            overlap[turn["speaker"]] += max(0.0, min(end, turn["end"]) - max(start, turn["start"]))
        # Microsecond rounding keeps equal overlaps equal despite float error.
        overlap = {k: round(v, 6) for k, v in overlap.items()}
        best = max(speakers, key=lambda s: overlap[s], default=None)
        speaker = best if best is not None and overlap[best] > 0 else None
        labeled.append({"speaker": speaker, "start": start, "end": end, "text": text})
    return speakers, labeled


def merge_transcript(diarization, transcript):
    """Join consecutive labeled segments of one speaker into turns."""
    speakers, labeled = label_segments(diarization, transcript)
    turns = []
    for item in labeled:
        if turns and turns[-1]["speaker"] == item["speaker"]:
            turns[-1]["end"] = max(turns[-1]["end"], item["end"])
            turns[-1]["text"] += " " + item["text"]
        else:
            turns.append(dict(item))
    unassigned = sum(item["speaker"] is None for item in labeled)
    return {"speakers": speakers, "turns": turns, "unassigned_segments": unassigned}


def clock(ms, separator):
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}{separator}{ms % 1000:03d}"


def cues(items, separator):
    """Yield (timing, speaker, text) per item; a cue lasts at least 1 ms and fits on one line."""
    for turn in items:
        start = round(turn["start"] * 1000)
        end = max(round(turn["end"] * 1000), start + 1)
        speaker = " ".join((turn["speaker"] or "").split()) or None
        yield f"{clock(start, separator)} --> {clock(end, separator)}", speaker, " ".join(turn["text"].split())


def to_webvtt(items):
    def escape(text):
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    blocks = [f"{timing}\n" + (f"<v {escape(speaker)}>" if speaker is not None else "") + escape(text)
              for timing, speaker, text in cues(items, ".")]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + "\n"


def to_srt(items):
    blocks = [f"{n}\n{timing}\n" + (f"{speaker}: " if speaker is not None else "") + text
              for n, (timing, speaker, text) in enumerate(cues(items, ","), 1)]
    return "\n\n".join(blocks) + "\n"


def to_rttm(diarization, file_id):
    def token(value):
        return re.sub(r"\s+", "_", value.strip()) or "_"
    return "".join(f"SPEAKER {token(file_id)} 1 {s['start']:.3f} {s['end'] - s['start']:.3f} <NA> <NA> "
                   f"{token(s['speaker'])} <NA> <NA>\n" for s in diarization["segments"])


MERGE_FORMATS = ("json", "vtt", "srt", "rttm")


def merge_files(args):
    fmt = getattr(args, "format", "json")
    names = ("diarization",) if fmt == "rttm" else ("diarization", "transcript")
    sources = [Path(getattr(args, name)) for name in names]
    if Path(args.output).resolve() in {p.resolve() for p in sources}:
        raise Blocked("Output must differ from the inputs")
    raw = [p.read_bytes() for p in sources]
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in zip(names, raw)}
    values = []
    for name, data in zip(names, raw):
        try:
            # Bytes let json accept a UTF-8 byte order mark.
            values.append(json.loads(data))
        except (ValueError, RecursionError):
            raise Blocked(f"{name.capitalize()} file is not valid UTF-8 JSON") from None
    if fmt == "rttm":
        try:
            validate_diarization(values[0])
        except (ValueError, TypeError):
            raise Blocked("Diarization input does not match the /v1/audio/diarize JSON shape") from None
        text = to_rttm(values[0], sources[0].stem)
    elif fmt in ("vtt", "srt"):
        # Subtitles keep each transcript segment's own timing.
        _, labeled = label_segments(*values)
        try:
            text = to_webvtt(labeled) if fmt == "vtt" else to_srt(labeled)
        except OverflowError:
            raise Blocked("Timestamps are too large to write as subtitles") from None
    else:
        merged = merge_transcript(*values)
        text = json.dumps(dict(merged, sources=hashes), ensure_ascii=False, indent=2)
    artifact = save_artifact(args.output, text.encode("utf-8"), "local:merge")
    artifact["provenance"]["sources"] = hashes
    return {"status": "completed", "artifacts": [artifact]}


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
    if args.command == "run":
        # A paid answer must have somewhere to go and must not replace its own input.
        output = Path(args.output)
        if args.file and output.resolve() == Path(args.file).resolve():
            raise Blocked("Artifact path must differ from the input file")
        if output.is_dir():
            raise Blocked("Artifact path is a directory")
        # atomic_write() replaces the path as given, in its own (unresolved) parent.
        parent = output.absolute().parent
        if not parent.is_dir() or not os.access(parent, os.W_OK):
            raise Blocked("Artifact directory is missing or not writable")
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
    post_timeout = diarize_timeout(units) if SERVICE == "diarize" and type(units) is int else REQUEST_TIMEOUT
    state["timeouts"] = {"request_seconds": post_timeout, "poll_seconds": args.timeout}
    try:
        if args.command == "run":
            method, url, body, content_type = submission(args)
            state["quota_observed"] = preflight(SERVICE, args.operation, key, units, model=getattr(args, "model", None))
            # An interruption from here until an ID is persisted requires reconciliation.
            state["status"] = "reconciliation_required"
            save_state(args.state, state)
            response = request(method, url, key, body, content_type, timeout=post_timeout)
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
    parser.add_argument("--format", choices=MERGE_FORMATS, help="merge output: json (default), vtt, srt, or rttm from the diarization alone")
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
    if args.command != "merge" and (args.diarization or args.transcript or args.format):
        parser.error("--diarization, --transcript and --format apply only to merge")
    args.format = args.format or "json"
    if args.command == "merge" and not args.diarization:
        parser.error("merge needs --diarization")
    if args.command == "merge" and bool(args.transcript) == (args.format == "rttm"):
        parser.error("merge needs --transcript, except --format rttm, which takes none")
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
