import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

SPEC = importlib.util.spec_from_file_location("client", Path(__file__).parents[1] / "scripts" / "client.py")
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)


def limits():
    return {"tier": "starter", "key": {"status": "ok", "billing_mode": "subscription"},
            "decision": {"can_request": False}}


def quota(service):
    window = {"day": {"remaining": 8}, "month": {"remaining": 12}}
    if service == "search":
        return {"tier": "starter", "search": dict(window, mode="tier"), "crawl": dict(window, mode="tier")}
    if service == "images":
        return {"tier": "starter", "img": window}
    return {"tier": "starter", "daily_pages": {"remaining": 8}, "monthly_pages": {"remaining": 12}}


class SafetyTests(unittest.TestCase):
    def test_auth_fallback_and_home(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODDY_HOME": tmp, "NEURALDEEP_API_KEY": "env-fixture"}, clear=True):
            p = Path(tmp) / "providers/neuraldeep/neuraldeep-auth.json"
            p.parent.mkdir(parents=True)
            self.assertEqual(client.resolve_key(), "env-fixture")
            for value in (None, "", "  ", "null", 1):
                p.write_text(json.dumps({"api_key": value}), encoding="utf-8")
                self.assertEqual(client.resolve_key(), "env-fixture")
            p.write_text('{"api_key": "file-fixture"}', encoding="utf-8")
            self.assertEqual(client.resolve_key(), "file-fixture")
            p.write_text('{}', encoding="utf-8")
            os.environ.pop("NEURALDEEP_API_KEY")
            with self.assertRaises(client.Blocked):
                client.resolve_key()

    def test_guard_ignores_chat_but_checks_service(self):
        for service, op in (("search", "web"), ("images", "generate"), ("ocr", "extract")):
            price = {"prices": [{"model": client.price_id(service, op), "premium": False}]}
            self.assertIsNone(client.guard(service, op, limits(), price, quota(service), 1))
            for bad in ({}, {"tier": "starter"}):
                with self.assertRaises(client.Blocked):
                    client.guard(service, op, limits(), price, bad, 1)
            price["prices"][0]["premium"] = True
            with self.assertRaises(client.Blocked):
                client.guard(service, op, limits(), price, quota(service), 1)

    def test_subscription_status_tier_and_units(self):
        price = {"prices": [{"model": "search:web", "premium": False}]}
        for field, value in (("billing_mode", "wallet"), ("status", "disabled")):
            lim = limits()
            lim["key"][field] = value
            with self.assertRaises(client.Blocked):
                client.guard("search", "web", lim, price, quota("search"), 1)
        for units in (0, -1, True, 9):
            with self.assertRaises(client.Blocked):
                client.guard("search", "web", limits(), price, quota("search"), units)
        lim = limits()
        lim["tier"] = "pro"
        with self.assertRaises(client.Blocked):
            client.guard("search", "web", lim, price, quota("search"), 1)

    def test_speechcore_is_blocked_without_verified_quota(self):
        with self.assertRaises(client.Blocked):
            client.guard("speechcore", "upload", limits(), {"prices": [{"model": "speechcore", "premium": False}]}, {}, 1)

    def test_whisper_is_blocked_without_verified_quota(self):
        price = {"prices": [{"model": "gigaam-v3", "premium": False}]}
        with self.assertRaises(client.Blocked) as raised:
            client.guard("whisper", "transcribe", limits(), price, {}, 1)
        self.assertIn("gigaam-v3 remaining-quota endpoint unverified", str(raised.exception))
        self.assertEqual(client.price_id("whisper", "transcribe"), "gigaam-v3")

    def test_transcription_models_have_their_own_price_id_and_block(self):
        for model in ("whisper-1", "whisper-podlodka-turbo", "gigaam-v3"):
            with self.subTest(model=model):
                self.assertEqual(client.price_id("whisper", "transcribe", model), model)
                price = {"prices": [{"model": model, "premium": False}]}
                with self.assertRaises(client.Blocked) as raised:
                    client.guard("whisper", "transcribe", limits(), price, {}, 1, model=model)
                self.assertIn(f"{model} remaining-quota endpoint unverified", str(raised.exception))
        self.assertEqual(client.price_id("whisper", "transcribe"), "gigaam-v3")

    def test_default_transcription_model_is_gigaam(self):
        import io
        import sys
        self.assertEqual(client.DEFAULT_TRANSCRIPTION_MODEL, "gigaam-v3")
        price = {"prices": [{"model": "gigaam-v3", "premium": False}]}
        with patch.object(client, "SERVICE", "whisper"), patch.object(client, "resolve_key", return_value="fixture"), \
                patch.object(client, "request", side_effect=[limits(), price]), \
                patch.object(sys, "argv", ["client.py", "check"]), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(client.main(), 1)
        self.assertIn("gigaam-v3 remaining-quota endpoint unverified", json.loads(out.getvalue())["errors"][0])

    def test_transcription_model_needs_its_own_price_row(self):
        price = {"prices": [{"model": "whisper-1", "premium": False}]}
        for model, reason in (("gigaam-v3", "non-premium"), ("whisper-2", "Unsupported transcription model")):
            with self.subTest(model=model), self.assertRaises(client.Blocked) as raised:
                client.guard("whisper", "transcribe", limits(), price, {}, 1, model=model)
            self.assertIn(reason, str(raised.exception))
            self.assertNotIn("unverified", str(raised.exception))

    def test_cli_model_flag_selects_the_transcription_model(self):
        import io
        import sys
        price = {"prices": [{"model": "gigaam-v3", "premium": False}]}
        with patch.object(client, "SERVICE", "whisper"), patch.object(client, "resolve_key", return_value="fixture"), \
                patch.object(client, "request", side_effect=[limits(), price]) as req, \
                patch.object(sys, "argv", ["client.py", "check", "--model", "gigaam-v3"]), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(client.main(), 1)
        self.assertIn("gigaam-v3 remaining-quota endpoint unverified", json.loads(out.getvalue())["errors"][0])
        self.assertEqual([c.args[:2] for c in req.call_args_list], [("GET", client.HUB + "/limits"), ("GET", client.PRICES)])
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "whisper"), patch.object(client, "resolve_key", return_value="fixture"), \
                patch.object(client, "request") as req, patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch.object(sys, "argv", ["client.py", "run", "--model", "whisper-podlodka-turbo", "--file", str(Path(tmp) / "a.wav"),
                                           "--state", str(Path(tmp) / "s.json"), "--output", str(Path(tmp) / "t.json")]):
            self.assertEqual(client.main(), 1)
        self.assertIn("whisper-podlodka-turbo transcription disabled", json.loads(out.getvalue())["errors"][0])
        req.assert_not_called()

    def test_model_flag_is_refused_outside_transcription_and_for_unknown_models(self):
        import io
        import sys
        for service, argv in (("search", ["check", "web", "--model", "gigaam-v3"]), ("whisper", ["check", "--model", "whisper-2"])):
            with self.subTest(service=service), patch.object(client, "SERVICE", service), patch.object(client, "request") as req, \
                    patch.object(sys, "argv", ["client.py", *argv]), patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as raised:
                    client.main()
            self.assertEqual(raised.exception.code, 2)
            req.assert_not_called()

    def test_poll_failure_and_deadline_never_fetch(self):
        for status in ("failed", "cancelled", "error"):
            get = Mock(return_value={"status": status})
            with self.assertRaises(client.JobError):
                client.poll(get, "/status", "/result", "completed", timeout=10)
            self.assertEqual(get.call_count, 1)
        get = Mock()
        with self.assertRaises(client.PollTimeout):
            client.poll(get, "/status", "/result", "completed", timeout=0)
        get.assert_not_called()

    def fake_clock(self):
        # sleep() advances a fake monotonic clock, so deadline loops end fast.
        now = [0.0]
        return (patch.object(client.time, "monotonic", side_effect=lambda: now[0]),
                patch.object(client.time, "sleep", side_effect=lambda s: now.__setitem__(0, now[0] + max(s, 1))))

    def test_unknown_intermediate_status_keeps_polling(self):
        # On 2026-10-17 live OCR jobs answered a status outside the known set before
        # "completed"; the helper marked finished jobs failed.
        get = Mock(side_effect=[{"status": "uploaded"}, {"status": "completed"}, b"artifact"])
        clock, sleep = self.fake_clock()
        with clock, sleep:
            self.assertEqual(client.poll(get, "/status", "/result", "completed", timeout=10), b"artifact")
        self.assertEqual([c.args[0] for c in get.call_args_list], ["/status", "/status", "/result"])

    def test_unknown_status_until_deadline_times_out_and_names_it(self):
        get = Mock(return_value={"status": "uploaded"})
        clock, sleep = self.fake_clock()
        with clock, sleep, self.assertRaises(client.PollTimeout) as raised:
            client.poll(get, "/status", "/result", "completed", timeout=10)
        self.assertIn("last status 'uploaded'", str(raised.exception))
        self.assertGreater(get.call_count, 1)
        self.assertTrue(all(c.args[0] == "/status" for c in get.call_args_list))

    def test_explicit_failure_statuses_stop_without_fetch(self):
        for status in ("failed", "cancelled", "canceled", "error", "expired", "rejected"):
            with self.subTest(status=status):
                get = Mock(return_value={"status": status})
                with self.assertRaises(client.JobError) as raised:
                    client.poll(get, "/status", "/result", "completed", timeout=10)
                self.assertIn(repr(status), str(raised.exception))
                self.assertEqual(get.call_count, 1)

    def test_missing_or_malformed_status_is_a_resumable_poll_error(self):
        for response in ({}, {"status": None}, {"status": 3}, [], "completed"):
            with self.subTest(response=response):
                get = Mock(return_value=response)
                with self.assertRaises(client.RequestError):
                    client.poll(get, "/status", "/result", "completed", timeout=10)
                self.assertEqual(get.call_count, 1)

    def test_unrecognized_status_text_is_not_echoed(self):
        get = Mock(return_value={"status": "Bearer secret-fixture\nX-Evil: 1"})
        clock, sleep = self.fake_clock()
        with clock, sleep, self.assertRaises(client.PollTimeout) as raised:
            client.poll(get, "/status", "/result", "completed", timeout=10)
        self.assertIn("last status unrecognized", str(raised.exception))
        self.assertNotIn("secret-fixture", str(raised.exception))

    def test_ocr_run_completes_through_unknown_intermediate_status(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "ocr"), patch.object(client, "resolve_key", return_value="fixture"), patch.object(client, "preflight", return_value={}), patch.object(client.time, "sleep"):
            args = self.args(tmp)
            args.operation, args.file = "extract", str(Path(tmp) / "invoice.pdf")
            Path(args.file).write_bytes(b"%PDF-1.4 fixture")
            content = json.dumps({"content": "# Invoice 4812"}).encode()
            responses = [{"id": "job-fixture"}, {"status": "uploaded"}, {"status": "completed"}, (content, "application/json")]
            with patch.object(client, "request", side_effect=responses) as req:
                result = client.execute(args)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["provider_job_id"], "job-fixture")
            self.assertEqual(Path(args.output).read_bytes(), content)
            self.assertEqual([c.args[0] for c in req.call_args_list], ["POST", "GET", "GET", "GET"])

    def test_poll_success_and_late_completion(self):
        get = Mock(side_effect=[{"status": "completed"}, b"artifact"])
        self.assertEqual(client.poll(get, "/status", "/result", "completed", timeout=10), b"artifact")
        get = Mock(return_value={"status": "completed"})
        with patch.object(client.time, "monotonic", side_effect=[0, 0, 11]):
            with self.assertRaises(client.PollTimeout):
                client.poll(get, "/status", "/result", "completed", timeout=10)
        self.assertEqual(get.call_count, 1)

    def test_http_error_and_post_timeout_not_retried_or_leaked(self):
        from urllib.error import HTTPError, URLError
        for error in (HTTPError("https://example.com", 503, "secret-fixture", {}, None),
                      HTTPError("https://example.com", 429, "secret-fixture", {"Retry-After": "1"}, None), URLError("secret-fixture")):
            opener = Mock()
            opener.open.side_effect = error
            with self.assertRaises(client.RequestError) as raised:
                client.request("POST", client.HUB + "/search/web", "secret-fixture", b"{}", opener=opener)
            self.assertNotIn("secret-fixture", str(raised.exception))
            self.assertEqual(opener.open.call_count, 1)

    def test_http_error_response_is_closed(self):
        import io
        from urllib.error import HTTPError
        body = io.BytesIO(b"private-body-fixture")
        opener = Mock()
        opener.open.side_effect = HTTPError("https://example.com", 503, "Unavailable", {}, body)
        with self.assertRaises(client.RequestError):
            client.request("GET", client.HUB + "/limits", "secret-fixture", opener=opener)
        self.assertTrue(body.closed)

    def test_durable_state_and_duplicate_prevention(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            state = client.new_state(path, "task-fixture", "images")
            state["provider_job_id"] = "provider-fixture"
            client.save_state(path, state)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["provider_job_id"], "provider-fixture")
            with self.assertRaises(client.Blocked) as raised:
                client.new_state(path, "task-fixture", "images")
            self.assertIn("State file already exists", str(raised.exception))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["provider_job_id"], "provider-fixture")

    def test_deadline_rechecked_before_download(self):
        get = Mock(return_value={"status": "completed"})
        with patch.object(client.time, "monotonic", side_effect=[0, 0, 9, 11]):
            with self.assertRaises(client.PollTimeout):
                client.poll(get, "/status", "/result", "completed", timeout=10)
        self.assertEqual(get.call_count, 1)

    def args(self, tmp):
        from argparse import Namespace
        return Namespace(command="run", operation="generate", pages=1, profile="fast",
                         payload=None, file=None, state=str(Path(tmp)/"state.json"),
                         output=str(Path(tmp)/"result.bin"), task_id="task-fixture", job_id=None, timeout=10)

    def test_preflight_http_failure_is_blocked_without_post(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "images"), patch.object(client, "resolve_key", return_value="fixture"):
            with patch.object(client, "request", side_effect=client.RequestError("HTTP 503")) as req:
                result = client.execute(self.args(tmp))
            self.assertEqual(result["status"], "blocked")
            self.assertEqual([c.args[0] for c in req.call_args_list], ["GET"])

    def test_ambiguous_submission_has_no_retry(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "images"), patch.object(client, "resolve_key", return_value="fixture"), patch.object(client, "preflight", return_value={}):
            with patch.object(client, "request", side_effect=client.RequestError("Transport error")) as req:
                result = client.execute(self.args(tmp))
            self.assertEqual(result["status"], "reconciliation_required")
            self.assertEqual(req.call_count, 1)
            self.assertEqual(result["artifacts"], [])

    def test_provider_id_saved_before_poll_and_failed_never_downloads(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "images"), patch.object(client, "resolve_key", return_value="fixture"), patch.object(client, "preflight", return_value={}):
            args = self.args(tmp)
            def respond(method, url, *a, **kw):
                if method == "POST":
                    self.assertEqual(json.loads(Path(args.state).read_text(encoding="utf-8"))["status"], "reconciliation_required")
                    return {"task_uid": "job-fixture"}
                self.assertEqual(json.loads(Path(args.state).read_text(encoding="utf-8"))["provider_job_id"], "job-fixture")
                self.assertFalse(url.endswith("/result"))
                return {"status": "failed"}
            with patch.object(client, "request", side_effect=respond) as req:
                result = client.execute(args)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(req.call_count, 2)
            self.assertFalse(Path(args.output).exists())

    def test_ocr_pro_requires_double_pages(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "ocr"), patch.object(client, "resolve_key", return_value="fixture"):
            args = self.args(tmp)
            args.command, args.operation, args.profile, args.pages = "check", "extract", "pro", 4
            with patch.object(client, "preflight", return_value={}) as pre:
                client.execute(args)
            self.assertEqual(pre.call_args.args[-1], 8)

    def test_shape_drift_and_boolean_remaining_block(self):
        price = {"prices": [{"model": "image:generate", "premium": False}]}
        for value in (None, True, "10", -1):
            q = quota("images")
            q["img"]["day"]["remaining"] = value
            with self.assertRaises(client.Blocked):
                client.guard("images", "generate", limits(), price, q, 1)

    def test_cli_preserves_sanitized_block_reason(self):
        import io
        import sys
        with patch.object(client, "execute", side_effect=client.Blocked("SpeechCore remaining-quota endpoint unverified; uploads disabled")), patch.object(sys, "argv", ["client.py", "check"]), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(client.main(), 1)
        self.assertIn("unverified", json.loads(out.getvalue())["errors"][0])

    def test_all_image_operations_are_async(self):
        for operation in client.OPERATIONS["images"]:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "images"), patch.object(client, "resolve_key", return_value="fixture"), patch.object(client, "preflight", return_value={}):
                args = self.args(tmp)
                args.operation = operation
                args.file = str(Path(tmp)/"input.png")
                Path(args.file).write_bytes(b"input-fixture")
                with patch.object(client, "request", side_effect=[{"task_uid": "job-fixture"}, {"status": "finished"}, (b"\x89PNG\r\n\x1a\n" + b"0" * 24, "image/png")]) as req:
                    result = client.execute(args)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["provider_job_id"], "job-fixture")
                self.assertTrue(Path(args.output).read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertEqual(req.call_args_list[0].args[0], "POST")
                self.assertTrue(req.call_args_list[1].args[1].endswith("/tasks/job-fixture"))
                self.assertTrue(req.call_args_list[2].args[1].endswith("/tasks/job-fixture/result"))

    def test_early_failure_has_handoff_fields(self):
        import io
        import sys
        with patch.object(client, "execute", side_effect=client.Blocked("No credential")), patch.object(sys, "argv", ["client.py", "run", "--task-id", "task-fixture"]), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(client.main(), 1)
        result = json.loads(out.getvalue())
        for field in ("task_id", "service", "provider_job_id", "status", "artifacts", "quota_observed", "timeouts", "errors"):
            self.assertIn(field, result)
        self.assertEqual(result["task_id"], "task-fixture")
        self.assertEqual(result["artifacts"], [])

    def test_incomplete_http_body_is_sanitized(self):
        from http.client import IncompleteRead
        opener = Mock()
        opener.open.side_effect = IncompleteRead(b"private-body-fixture", 100)
        with self.assertRaises(client.RequestError) as raised:
            client.request("POST", client.HUB + "/search/web", "secret-fixture", b"{}", opener=opener)
        self.assertNotIn("private-body-fixture", str(raised.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_result_validation_rejects_false_success_and_preserves_job(self):
        invalid = [("images", (b'{"success":false,"error":"bad"}', "application/json")),
                   ("ocr", (b"<html>maintenance</html>", "text/html")),
                   ("speechcore", (b"", "application/json"))]
        for service, response in invalid:
            with self.subTest(service=service), tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", service), patch.object(client, "resolve_key", return_value="fixture"):
                args = self.args(tmp)
                args.command, args.job_id = "resume", "job-fixture"
                with patch.object(client, "poll", return_value=response):
                    result = client.execute(args)
                self.assertEqual(result["status"], "poll_error")
                self.assertEqual(result["provider_job_id"], "job-fixture")
                self.assertEqual(result["artifacts"], [])
                self.assertFalse(Path(args.output).exists())

    def test_valid_structured_results_and_wrong_types(self):
        for service, value in [("ocr", {"content": "recognized text"}),
                               ("speechcore", {"segments": [{"text": "hello", "start": 0, "end": 1}], "duration": 1})]:
            data = json.dumps(value).encode()
            self.assertEqual(client.validate_result(service, (data, "application/json")), data)
            for bad in ({"error": "unavailable"}, {"success": False}, {}, []):
                with self.assertRaises(client.RequestError):
                    client.validate_result(service, (json.dumps(bad).encode(), "application/json"))
        with self.assertRaises(client.RequestError):
            client.validate_result("images", (b"not PNG", "image/png"))

    def transcript(self):
        return json.dumps({"detected_language": "en", "duration": 2.5,
                           "segments": [{"speaker": "SPEAKER_00", "text": "hello", "start": 0, "end": 2.5}]}).encode()

    def test_speechcore_resume_polls_until_completed_and_saves_transcript(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "speechcore"), patch.object(client, "resolve_key", return_value="fixture"), patch.object(client.time, "sleep"):
            args = self.args(tmp)
            args.command, args.operation, args.job_id = "resume", "upload", "job-fixture"
            data = self.transcript()
            responses = [{"status": "processing", "progress": 45}, {"status": "completed", "progress": 100},
                         (data, "application/json")]
            with patch.object(client, "request", side_effect=responses) as req:
                result = client.execute(args)
            base = client.SPEECH + "/transcriptions/job-fixture"
            self.assertEqual([c.args[:2] for c in req.call_args_list],
                             [("GET", base + "/status"), ("GET", base + "/status"), ("GET", base)])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["provider_job_id"], "job-fixture")
            self.assertEqual(Path(args.output).read_bytes(), data)
            self.assertEqual(result["artifacts"][0]["sha256"], client.hashlib.sha256(data).hexdigest())
            self.assertEqual(result["artifacts"][0]["provenance"]["endpoint"], base)
            self.assertEqual(json.loads(Path(args.state).read_text(encoding="utf-8"))["status"], "completed")

    def test_resume_from_existing_state_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "speechcore"), patch.object(client, "resolve_key", return_value="fixture"):
            args = self.args(tmp)
            args.command, args.operation = "resume", "upload"
            state = client.state_template("task-fixture", "speechcore")
            state.update(provider_job_id="job-fixture", status="timed_out", errors=["Polling deadline reached"])
            client.save_state(args.state, state)
            with patch.object(client, "request", side_effect=[{"status": "completed"}, (self.transcript(), "application/json")]) as req:
                result = client.execute(args)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["provider_job_id"], "job-fixture")
            self.assertEqual(req.call_count, 2)
            self.assertTrue(all(c.args[0] == "GET" for c in req.call_args_list))
            # Earlier diagnostics are kept for the handoff.
            self.assertEqual(result["errors"], ["Polling deadline reached"])

    def test_resume_refuses_non_resumable_state_without_requests(self):
        for status in ("completed", "blocked", "failed", "reconciliation_required"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "speechcore"), patch.object(client, "resolve_key", return_value="fixture"):
                args = self.args(tmp)
                args.command, args.operation = "resume", "upload"
                state = client.state_template("task-fixture", "speechcore")
                state.update(provider_job_id="job-fixture", status=status)
                client.save_state(args.state, state)
                with patch.object(client, "request") as req:
                    with self.assertRaises(client.Blocked):
                        client.execute(args)
                req.assert_not_called()

    def test_submission_reads_service_specific_job_id_field(self):
        cases = [("speechcore", "upload", "task_id", {"status": "completed"}, (self.transcript(), "application/json")),
                 ("ocr", "extract", "id", {"status": "completed"}, (b'{"content": "text"}', "application/json")),
                 ("images", "generate", "task_uid", {"status": "finished"}, (b"\x89PNG\r\n\x1a\n" + b"0" * 24, "image/png"))]
        for service, operation, field, status, result_body in cases:
            with self.subTest(service=service), tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", service), patch.object(client, "resolve_key", return_value="fixture"), patch.object(client, "preflight", return_value={}), patch.object(client, "submission", return_value=("POST", "https://api.neuraldeep.ru/v1/x", b"", "application/json")):
                args = self.args(tmp)
                args.operation = operation
                with patch.object(client, "request", side_effect=[{field: "job-fixture"}, status, result_body]):
                    result = client.execute(args)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["provider_job_id"], "job-fixture")
        # A field belonging to another service is not accepted as the provider ID.
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "speechcore"), patch.object(client, "resolve_key", return_value="fixture"), patch.object(client, "preflight", return_value={}), patch.object(client, "submission", return_value=("POST", client.SPEECH + "/upload", b"", "application/json")):
            args = self.args(tmp)
            args.operation = "upload"
            with patch.object(client, "request", return_value={"id": "job-fixture"}) as req:
                result = client.execute(args)
            self.assertEqual(result["status"], "reconciliation_required")
            self.assertIsNone(result["provider_job_id"])
            self.assertEqual(req.call_count, 1)

    def test_artifact_has_hash_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = client.save_artifact(Path(tmp) / "result.bin", b"test", "/result")
            self.assertEqual(artifact["sha256"], "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08")
            self.assertEqual(artifact["provenance"]["endpoint"], "/result")
            self.assertEqual(artifact["provenance"]["trust"], "untrusted_data")

    def test_multipart_filename_cannot_inject_headers(self):
        cases = {"photo.JPEG": "upload.JPEG", "x.png\r\nX-Evil: 1": "upload", 'a.p"ng': "upload",
                 "b.png; name=evil": "upload", "c.png\nX-Evil: 1": "upload", "noext": "upload"}
        for name, expected in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / name
                path.write_bytes(b"input-fixture")
                body, content_type = client.multipart(path, "image", {})
                part = body.split(b"--" + content_type.split("; boundary=")[1].encode())[1]
                head = part.removeprefix(b"\r\n").split(b"\r\n\r\n", 1)[0].decode()
                self.assertEqual(head.split("\r\n"), [f'Content-Disposition: form-data; name="image"; filename="{expected}"',
                                                      "Content-Type: application/octet-stream"])
                self.assertNotIn(b"X-Evil", body)

    def test_existing_state_path_is_refused_with_clear_reason(self):
        import io
        import sys
        cases = [["run", client.OPERATIONS[client.SERVICE][0]]]
        if client.SERVICE not in client.SYNCHRONOUS:
            # Synchronous services refuse --job-id before opening any state file.
            cases.append(["resume", "--job-id", "job-other"])
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "resolve_key", return_value="fixture"):
            state = Path(tmp) / "state.json"
            state.write_text('{"provider_job_id": "job-fixture"}', encoding="utf-8")
            paths = ["--state", str(state), "--output", str(Path(tmp) / "result.json")]
            for argv in cases:
                with self.subTest(argv=argv), patch.object(client, "request") as req, patch.object(sys, "argv", ["client.py", *argv, *paths]), patch("sys.stdout", new_callable=io.StringIO) as out:
                    self.assertEqual(client.main(), 1)
                result = json.loads(out.getvalue())
                self.assertEqual(result["status"], "blocked")
                self.assertIn("State file already exists", result["errors"][0])
                req.assert_not_called()
                self.assertEqual(state.read_text(encoding="utf-8"), '{"provider_job_id": "job-fixture"}')

    def test_polling_defaults_come_from_shared_constants(self):
        import inspect
        import sys
        self.assertEqual((client.POLL_INTERVAL, client.DEFAULT_TIMEOUT), (2, 120))
        params = inspect.signature(client.poll).parameters
        self.assertEqual((params["interval"].default, params["timeout"].default),
                         (client.POLL_INTERVAL, client.DEFAULT_TIMEOUT))
        with patch.object(client, "execute", return_value={"status": "ready"}) as execute, patch.object(sys, "argv", ["client.py", "check"]), patch("sys.stdout"):
            client.main()
        self.assertEqual(execute.call_args.args[0].timeout, client.DEFAULT_TIMEOUT)

    def test_text_files_are_read_as_utf8_on_non_utf8_locales(self):
        import subprocess
        import sys
        import textwrap
        # Runs in a C locale, whose default text encoding is ASCII, so any
        # read that relies on the locale fails on non-ASCII content.
        script = textwrap.dedent('''
            import importlib.util, json, sys
            from argparse import Namespace
            from pathlib import Path
            from unittest.mock import patch
            spec = importlib.util.spec_from_file_location("client", sys.argv[1])
            client = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(client)
            tmp, text, out = Path(sys.argv[2]), "\\u043f\\u0440\\u0438\\u0432\\u0435\\u0442", {}
            def attempt(name, fn):
                try:
                    out[name] = fn()
                except Exception as error:
                    out[name] = type(error).__name__
            def utf8_json(path, value):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            utf8_json(tmp / "providers/neuraldeep/neuraldeep-auth.json", {"api_key": "file-fixture", "name": text})
            with patch.dict("os.environ", {"CODDY_HOME": str(tmp)}, clear=True):
                attempt("key", client.resolve_key)
            utf8_json(tmp / "payload.json", {"query": text})
            with patch.object(client, "SERVICE", "search"):
                args = Namespace(payload=str(tmp / "payload.json"), operation="web", file=None, profile="fast")
                attempt("query", lambda: json.loads(client.submission(args)[2])["query"] == text)
            state = dict(client.state_template("task-fixture", "ocr"), provider_job_id="job-fixture",
                         status="timed_out", errors=[text])
            utf8_json(tmp / "state.json", state)
            args = Namespace(command="resume", operation="extract", pages=1, profile="fast", payload=None, file=None,
                             state=str(tmp / "state.json"), output=str(tmp / "result.json"), task_id="task-fixture",
                             job_id=None, timeout=10)
            responses = [{"status": "completed"}, (b'{"content": "text"}', "application/json")]
            with patch.object(client, "SERVICE", "ocr"), patch.object(client, "resolve_key", return_value="fixture"), \\
                    patch.object(client, "request", side_effect=responses):
                attempt("resume", lambda: client.execute(args)["status"])
            print(json.dumps(out))
        ''')
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LC_", "PYTHON"))}
        env.update(LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
        with tempfile.TemporaryDirectory() as tmp:
            run = subprocess.run([sys.executable, "-B", "-c", script, str(SPEC.origin), tmp],
                                 env=env, capture_output=True, encoding="utf-8")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), {"key": "file-fixture", "query": True, "resume": "completed"})

    def test_request_refuses_unapproved_origins_before_sending(self):
        for url in ("http://api.neuraldeep.ru/v1/limits", "https://evil.example/v1/limits",
                    "https://api.neuraldeep.ru.evil.example/v1/limits", "https://api.neuraldeep.ru:8443/v1/limits",
                    "https://user@api.neuraldeep.ru/v1/limits", "file:///etc/passwd"):
            with self.subTest(url=url):
                opener = MagicMock()
                with self.assertRaises(client.Blocked):
                    client.request("GET", url, "secret-fixture", opener=opener)
                opener.open.assert_not_called()

    def test_key_sent_to_api_but_not_to_public_prices(self):
        for url, authorized in ((client.HUB + "/limits", True), (client.PRICES, False)):
            with self.subTest(url=url):
                opener = MagicMock()
                response = opener.open.return_value.__enter__.return_value
                response.read.return_value = b"{}"
                response.headers.get.return_value = "application/json"
                self.assertEqual(client.request("GET", url, "secret-fixture", opener=opener), {})
                sent = opener.open.call_args.args[0]
                self.assertEqual(sent.get_header("Authorization"), "Bearer secret-fixture" if authorized else None)

    def test_redirects_are_not_followed(self):
        import io
        from email.message import Message
        from urllib.request import HTTPSHandler, build_opener
        from urllib.response import addinfourl

        class RedirectingHandler(HTTPSHandler):
            def __init__(self):
                super().__init__()
                self.calls = []

            def https_open(self, req):
                self.calls.append(req.full_url)
                headers = Message()
                headers["Location"] = "https://evil.example/collect"
                response = addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
                response.msg = "Found"
                return response

        handler = RedirectingHandler()
        # Wrap the helper's own default opener so the real NoRedirect handler is exercised.
        with patch.object(client, "build_opener", side_effect=lambda *h: build_opener(*h, handler)):
            with self.assertRaises(client.RequestError) as raised:
                client.request("POST", client.HUB + "/images/generate", "secret-fixture", b"{}")
        self.assertIn("HTTP 302", str(raised.exception))
        self.assertEqual(handler.calls, [client.HUB + "/images/generate"])

    def test_timeout_then_resume_from_saved_state_without_resubmitting(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 24
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "images"), patch.object(client, "resolve_key", return_value="fixture"), patch.object(client, "preflight", return_value={}) as pre:
            args = self.args(tmp)
            # Deadline set at 0; the fifth clock read is past the 10-second timeout.
            clock = iter([0, 0, 0, 0, 11])
            with patch.object(client, "request", side_effect=[{"task_uid": "job-fixture"}, {"status": "processing"}]) as req, patch.object(client.time, "monotonic", side_effect=lambda: next(clock)), patch.object(client.time, "sleep"):
                result = client.execute(args)
            self.assertEqual(result["status"], "timed_out")
            self.assertEqual(result["provider_job_id"], "job-fixture")
            self.assertEqual(result["artifacts"], [])
            self.assertEqual(req.call_count, 2)
            self.assertFalse(Path(args.output).exists())
            saved = json.loads(Path(args.state).read_text(encoding="utf-8"))
            self.assertEqual((saved["status"], saved["provider_job_id"]), ("timed_out", "job-fixture"))

            args.command = "resume"
            with patch.object(client, "request", side_effect=[{"status": "finished"}, (png, "image/png")]) as req:
                result = client.execute(args)
            self.assertEqual(result["status"], "completed")
            self.assertEqual([c.args[0] for c in req.call_args_list], ["GET", "GET"])
            self.assertTrue(req.call_args_list[0].args[1].endswith("/tasks/job-fixture"))
            self.assertEqual(pre.call_count, 1)
            self.assertEqual(Path(args.output).read_bytes(), png)
            self.assertEqual(json.loads(Path(args.state).read_text(encoding="utf-8"))["status"], "completed")

    def test_resume_refuses_non_resumable_state(self):
        cases = [("images", s) for s in ("reconciliation_required", "failed", "completed", "blocked")] + [("ocr", "timed_out")]
        for service, status in cases:
            with self.subTest(service=service, status=status), tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "images"), patch.object(client, "resolve_key", return_value="fixture"):
                args = self.args(tmp)
                args.command = "resume"
                state = client.state_template("task-fixture", service)
                state.update(status=status, provider_job_id="job-fixture")
                client.save_state(args.state, state)
                before = Path(args.state).read_bytes()
                with patch.object(client, "request") as req, self.assertRaises(client.Blocked):
                    client.execute(args)
                req.assert_not_called()
                self.assertEqual(Path(args.state).read_bytes(), before)

    def test_multipart_body_format(self):
        content = b"\x89PNG\r\n\x1a\n\x00--not-a-boundary\r\n\r\nbinary"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private-name.png"
            path.write_bytes(content)
            body, content_type = client.multipart(path, "image", {})
            ocr_body, ocr_type = client.multipart(path, "file", {"model_profile": "pro"})
        media, boundary = content_type.split("; boundary=")
        self.assertEqual(media, "multipart/form-data")
        delimiter = b"--" + boundary.encode()
        parts = body.split(delimiter)
        self.assertEqual((len(parts), parts[0], parts[-1]), (3, b"", b"--\r\n"))
        head, data = parts[1].removeprefix(b"\r\n").split(b"\r\n\r\n", 1)
        self.assertEqual(head.decode().split("\r\n"), ['Content-Disposition: form-data; name="image"; filename="upload.png"',
                                                      "Content-Type: application/octet-stream"])
        self.assertEqual(data, content + b"\r\n")
        self.assertNotIn(b"private-name", body)
        ocr_parts = ocr_body.split(b"--" + ocr_type.split("; boundary=")[1].encode())
        self.assertEqual(ocr_parts[1], b'\r\nContent-Disposition: form-data; name="model_profile"\r\n\r\npro\r\n')
        self.assertIn(b'name="file"; filename="upload.png"', ocr_parts[2])
        self.assertNotEqual(boundary, ocr_type.split("; boundary=")[1])

    def test_image_submission_routing(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "images"):
            args = self.args(tmp)
            args.payload = str(Path(tmp) / "request.json")
            Path(args.payload).write_text('{"prompt": "fixture"}', encoding="utf-8")
            method, url, body, content_type = client.submission(args)
            self.assertEqual((method, url, json.loads(body), content_type),
                             ("POST", client.HUB + "/images/generate", {"prompt": "fixture"}, "application/json"))
            args.operation = "upscale"
            with self.assertRaises(client.Blocked):
                client.submission(args)
            args.file = str(Path(tmp) / "input.png")
            Path(args.file).write_bytes(b"input-fixture")
            with self.assertRaises(client.Blocked):
                client.submission(args)
            args.payload = None
            method, url, body, content_type = client.submission(args)
            self.assertEqual((method, url), ("POST", client.HUB + "/images/upscale"))
            self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
            self.assertIn(b'name="image"; filename="upload.png"', body)

    def test_resume_of_edited_resumable_state_is_blocked_not_crashed(self):
        import io
        import sys
        for status in ("submitted", "timed_out", "poll_error"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp, patch.object(client, "resolve_key", return_value="fixture"):
                state, output = Path(tmp) / "state.json", Path(tmp) / "result.json"
                saved = client.state_template("task-fixture", "search")
                saved.update(status=status, provider_job_id="job-fixture")
                client.save_state(state, saved)
                before = state.read_bytes()
                argv = ["client.py", "resume", "--state", str(state), "--output", str(output)]
                with patch.object(client, "SERVICE", "search"), patch.object(client, "request") as req, patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO) as out:
                    self.assertEqual(client.main(), 1)
                result = json.loads(out.getvalue())
                self.assertEqual(result["status"], "blocked")
                self.assertIn("no resumable job endpoint", result["errors"][0])
                req.assert_not_called()
                self.assertFalse(output.exists())
                self.assertEqual(state.read_bytes(), before)

    def test_resume_of_malformed_state_is_blocked_not_crashed(self):
        import io
        import sys
        cases = ["[]", "null", '"x"', '{"service": "speechcore", "status": []}',
                 '{"service": "speechcore", "status": "submitted", "provider_job_id": "job-fixture", "errors": "x"}']
        for text in cases:
            with self.subTest(state=text), tempfile.TemporaryDirectory() as tmp, patch.object(client, "resolve_key", return_value="fixture"):
                state, output = Path(tmp) / "state.json", Path(tmp) / "result.json"
                state.write_text(text, encoding="utf-8")
                argv = ["client.py", "resume", "--state", str(state), "--output", str(output)]
                with patch.object(client, "SERVICE", "speechcore"), patch.object(client, "request") as req, patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO) as out:
                    self.assertEqual(client.main(), 1)
                result = json.loads(out.getvalue())
                self.assertEqual(result["status"], "blocked")
                self.assertIn("not resumable", result["errors"][0])
                req.assert_not_called()
                self.assertFalse(output.exists())
                self.assertEqual(state.read_text(encoding="utf-8"), text)

    def test_whisper_is_synchronous_and_refuses_resume(self):
        import io
        import sys
        self.assertIn("whisper", client.SYNCHRONOUS)
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "whisper"), patch.object(client, "resolve_key", return_value="fixture"):
            state = Path(tmp) / "state.json"
            argv = ["client.py", "resume", "--job-id", "job-fixture", "--state", str(state), "--output", str(Path(tmp) / "result.json")]
            with patch.object(client, "request") as req, patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(client.main(), 1)
            result = json.loads(out.getvalue())
            self.assertEqual(result["status"], "blocked")
            self.assertIn("Synchronous whisper has no resumable job endpoint", result["errors"][0])
            self.assertFalse(state.exists())
            req.assert_not_called()

    def test_whisper_run_is_blocked_before_any_request(self):
        import io
        import sys
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "whisper"), patch.object(client, "resolve_key", return_value="fixture"):
            output = Path(tmp) / "result.json"
            argv = ["client.py", "run", "transcribe", "--file", str(Path(tmp) / "missing.wav"),
                    "--state", str(Path(tmp) / "state.json"), "--output", str(output)]
            with patch.object(client, "request") as req, patch.object(client, "multipart") as body, patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(client.main(), 1)
            result = json.loads(out.getvalue())
            self.assertEqual(result["status"], "blocked")
            self.assertIn("gigaam-v3 transcription disabled until remaining quota is verified", result["errors"][0])
            self.assertFalse(output.exists())
            req.assert_not_called()
            body.assert_not_called()


if __name__ == "__main__":
    unittest.main()
