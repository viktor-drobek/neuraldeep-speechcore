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



def stat_is_fifo(path):
    import stat
    return stat.S_ISFIFO(os.lstat(path).st_mode)


def drain(body):
    """Read a streamed upload body the way http.client does, block by block."""
    return b"".join(iter(lambda: body.read(8192), b""))
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
    def test_keys_come_from_the_skill_variable_then_the_shared_one(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODDY_HOME": tmp}, clear=True):
            dotenv = Path(tmp) / ".env"
            # The Coddy provider file is no longer a source.
            auth = Path(tmp) / "providers/neuraldeep/neuraldeep-auth.json"
            auth.parent.mkdir(parents=True)
            auth.write_text('{"api_key": "provider-file"}', encoding="utf-8")
            with self.assertRaises(client.Blocked) as raised:
                client.resolve_keys("diarize")
            self.assertIn("NEURALDEEP_DIARIZATION_API_KEY", str(raised.exception))
            self.assertIn(".env", str(raised.exception))
            dotenv.write_text("NEURALDEEP_API_KEY=shared-file\n", encoding="utf-8")
            self.assertEqual(client.resolve_keys("diarize"), ("NEURALDEEP_API_KEY", ["shared-file"]))
            dotenv.write_text("NEURALDEEP_API_KEY=shared-file\nexport NEURALDEEP_DIARIZATION_API_KEY='own-a, own-b'\n",
                              encoding="utf-8")
            self.assertEqual(client.resolve_keys("diarize"), ("NEURALDEEP_DIARIZATION_API_KEY", ["own-a", "own-b"]))
            os.environ["NEURALDEEP_DIARIZATION_API_KEY"] = "own-env"  # the environment wins, as in Coddy
            self.assertEqual(client.resolve_keys("diarize"), ("NEURALDEEP_DIARIZATION_API_KEY", ["own-env"]))
            for unusable in ("", "  ", "null", " , bad key ,NULL"):
                os.environ["NEURALDEEP_DIARIZATION_API_KEY"] = unusable
                self.assertEqual(client.resolve_keys("diarize"), ("NEURALDEEP_DIARIZATION_API_KEY", ["own-a", "own-b"]))
            os.environ["NEURALDEEP_API_KEY"] = "shared-env"
            dotenv.write_text("NEURALDEEP_SEARCH_API_KEY=search-only\n", encoding="utf-8")
            self.assertEqual(client.resolve_keys("diarize"), ("NEURALDEEP_API_KEY", ["shared-env"]))
            self.assertEqual(client.resolve_keys("search"), ("NEURALDEEP_SEARCH_API_KEY", ["search-only"]))
            self.assertEqual(client.KEY_VARIABLES, {
                "search": "NEURALDEEP_SEARCH_API_KEY", "ocr": "NEURALDEEP_OCR_API_KEY",
                "images": "NEURALDEEP_IMAGES_API_KEY", "speechcore": "NEURALDEEP_SPEECHCORE_API_KEY",
                "whisper": "NEURALDEEP_TRANSCRIPTIONS_API_KEY", "diarize": "NEURALDEEP_DIARIZATION_API_KEY"})
            self.assertEqual(client.resolve_keys(), client.resolve_keys(client.SERVICE))

    def test_dotenv_lines_parse_like_coddy(self):
        # The cases of coddy-agent internal/config/dotenv_test.go.
        cases = [("KEY=value", ("KEY", "value")), ("KEY=", ("KEY", "")), ("export KEY=value", ("KEY", "value")),
                 ("export  KEY=value", ("KEY", "value")), ('KEY="hello world"', ("KEY", "hello world")),
                 ("KEY='hello world'", ("KEY", "hello world")), ('KEY="a\\nb \\"q\\""', ("KEY", 'a\nb "q"')),
                 ("KEY=value # comment", ("KEY", "value")), ("KEY=  value  ", ("KEY", "value")),
                 ("# this is a comment", None), ("  # indented comment", None), ("", None), ("   ", None),
                 ("KEYONLY", None), ("KEY NAME=val", None), ("=value", None)]
        for line, expected in cases:
            with self.subTest(line=line):
                self.assertEqual(client.parse_dotenv_line(line), expected)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".env").write_text("A=first\nA=second\nB='x'\n", encoding="utf-8")
            self.assertEqual(client.read_dotenv(tmp), {"A": "first", "B": "x"})  # the first one wins, as in Coddy
            self.assertEqual(client.read_dotenv(Path(tmp) / "missing"), {})

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
        with patch.object(client, "SERVICE", "whisper"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), \
                patch.object(client, "request", side_effect=[limits(), price]), \
                patch.object(sys, "argv", ["client.py", "check", "--minutes", "1"]), patch("sys.stdout", new_callable=io.StringIO) as out:
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
        with patch.object(client, "SERVICE", "whisper"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), \
                patch.object(client, "request", side_effect=[limits(), price]) as req, \
                patch.object(sys, "argv", ["client.py", "check", "--model", "gigaam-v3", "--minutes", "1"]), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(client.main(), 1)
        self.assertIn("gigaam-v3 remaining-quota endpoint unverified", json.loads(out.getvalue())["errors"][0])
        self.assertEqual([c.args[:2] for c in req.call_args_list], [("GET", client.HUB + "/limits"), ("GET", client.PRICES)])
        podlodka = {"prices": [{"model": "whisper-podlodka-turbo", "premium": False}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "whisper"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), \
                patch.object(client, "request", side_effect=[limits(), podlodka]) as req, patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch.object(sys, "argv", ["client.py", "run", "--model", "whisper-podlodka-turbo", "--file", str(Path(tmp) / "a.wav"), "--minutes", "1",
                                           "--state", str(Path(tmp) / "s.json"), "--output", str(Path(tmp) / "t.json")]):
            Path(tmp, "a.wav").write_bytes(b"RIFF")
            self.assertEqual(client.main(), 1)
        self.assertIn("whisper-podlodka-turbo remaining-quota endpoint unverified", json.loads(out.getvalue())["errors"][0])
        self.assertNotIn("POST", [c.args[0] for c in req.call_args_list])

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
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "ocr"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), patch.object(client, "preflight", return_value={}), patch.object(client.time, "sleep"):
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
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "images"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
            with patch.object(client, "request", side_effect=client.RequestError("HTTP 503")) as req:
                result = client.execute(self.args(tmp))
            self.assertEqual(result["status"], "blocked")
            self.assertEqual([c.args[0] for c in req.call_args_list], ["GET"])

    def test_ambiguous_submission_has_no_retry(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "images"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), patch.object(client, "preflight", return_value={}):
            with patch.object(client, "request", side_effect=client.RequestError("Transport error")) as req:
                result = client.execute(self.args(tmp))
            self.assertEqual(result["status"], "reconciliation_required")
            self.assertEqual(req.call_count, 1)
            self.assertEqual(result["artifacts"], [])

    def test_provider_id_saved_before_poll_and_failed_never_downloads(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "images"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), patch.object(client, "preflight", return_value={}):
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
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "ocr"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
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
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "images"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), patch.object(client, "preflight", return_value={}):
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
            with self.subTest(service=service), tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", service), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
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
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "speechcore"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), patch.object(client.time, "sleep"):
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
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "speechcore"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
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
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "speechcore"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
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
            with self.subTest(service=service), tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", service), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), patch.object(client, "preflight", return_value={}), patch.object(client, "submission", return_value=("POST", "https://api.neuraldeep.ru/v1/x", b"", "application/json")):
                args = self.args(tmp)
                args.operation = operation
                with patch.object(client, "request", side_effect=[{field: "job-fixture"}, status, result_body]):
                    result = client.execute(args)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["provider_job_id"], "job-fixture")
        # A field belonging to another service is not accepted as the provider ID.
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "speechcore"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), patch.object(client, "preflight", return_value={}), patch.object(client, "submission", return_value=("POST", client.SPEECH + "/upload", b"", "application/json")):
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
                body = drain(body)
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
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
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
            (tmp / ".env").write_bytes(("# " + text + "\\nNAME=" + text + "\\nNEURALDEEP_OCR_API_KEY=file-fixture\\n").encode("utf-8"))
            with patch.dict("os.environ", {"CODDY_HOME": str(tmp)}, clear=True):
                attempt("key", lambda: client.resolve_keys("ocr")[1][0])
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
            with patch.object(client, "SERVICE", "ocr"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), \\
                    patch.object(client, "request", side_effect=responses):
                attempt("resume", lambda: client.execute(args)["status"])
            utf8_json(tmp / "d.json", {"duration": 2.0, "num_speakers": 1, "minutes_charged": 1,
                                       "segments": [{"speaker": "SPEAKER_0", "start": 0, "end": 2}]})
            utf8_json(tmp / "t.json", {"segments": [{"start": 0, "end": 2, "text": text}]})
            args = Namespace(command="merge", diarization=str(tmp / "d.json"), transcript=str(tmp / "t.json"),
                             output=str(tmp / "merged.json"), task_id="task-fixture")
            def merge():
                client.execute(args)
                args.format, args.output = "vtt", str(tmp / "merged.vtt")
                client.execute(args)
                return (json.loads((tmp / "merged.json").read_bytes().decode("utf-8"))["turns"][0]["text"] == text
                        and text in (tmp / "merged.vtt").read_bytes().decode("utf-8"))
            with patch.object(client, "SERVICE", "diarize"):
                attempt("merge", merge)
            print(json.dumps(out))
        ''')
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LC_", "PYTHON"))}
        env.update(LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
        with tempfile.TemporaryDirectory() as tmp:
            run = subprocess.run([sys.executable, "-B", "-c", script, str(SPEC.origin), tmp],
                                 env=env, capture_output=True, encoding="utf-8")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), {"key": "file-fixture", "query": True, "resume": "completed", "merge": True})

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
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "images"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), patch.object(client, "preflight", return_value={}) as pre:
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
            with self.subTest(service=service, status=status), tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "images"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
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
            body = drain(body)
            ocr_body, ocr_type = client.multipart(path, "file", {"model_profile": "pro"})
            ocr_body = drain(ocr_body)
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
            self.assertIn(b'name="image"; filename="upload.png"', drain(body))

    def test_resume_of_edited_resumable_state_is_blocked_not_crashed(self):
        import io
        import sys
        for status in ("submitted", "timed_out", "poll_error"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp, patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
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
            with self.subTest(state=text), tempfile.TemporaryDirectory() as tmp, patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
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
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "whisper"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
            state = Path(tmp) / "state.json"
            argv = ["client.py", "resume", "--job-id", "job-fixture", "--state", str(state), "--output", str(Path(tmp) / "result.json")]
            with patch.object(client, "request") as req, patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(client.main(), 1)
            result = json.loads(out.getvalue())
            self.assertEqual(result["status"], "blocked")
            self.assertIn("Synchronous whisper has no resumable job endpoint", result["errors"][0])
            self.assertFalse(state.exists())
            req.assert_not_called()

    def test_subscription_transcription_is_blocked_before_any_post(self):
        import io
        import sys
        price = {"prices": [{"model": "gigaam-v3", "premium": False}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "whisper"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
            output = Path(tmp) / "result.json"
            Path(tmp, "a.wav").write_bytes(b"RIFF")
            argv = ["client.py", "run", "transcribe", "--file", str(Path(tmp) / "a.wav"), "--minutes", "1",
                    "--state", str(Path(tmp) / "state.json"), "--output", str(output)]
            with patch.object(client, "request", side_effect=[limits(), price]) as req, patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(client.main(), 1)
            result = json.loads(out.getvalue())
            self.assertEqual(result["status"], "blocked")
            self.assertIn("gigaam-v3 remaining-quota endpoint unverified", result["errors"][0])
            self.assertIn("wallet", result["errors"][0])
            self.assertFalse(output.exists())
            self.assertNotIn("POST", [c.args[0] for c in req.call_args_list])

    # Speaker diarization (/v1/audio/diarize), verified read-only on 2026-10-18:
    # the quota reports used and limit minutes per window, not a remaining count.
    def diarize_quota(self, day_used=0, month_used=0, limit=300):
        # Synthetic limits; live values are never fixtures.
        return {"unit": "minute", "tier": "starter", "day": {"used": day_used, "limit": limit},
                "month": {"used": month_used, "limit": limit * 10}}

    def diarization(self):
        return {"duration": 61.0, "num_speakers": 2, "minutes_charged": 2,
                "segments": [{"speaker": "SPEAKER_0", "start": 1.1, "end": 5.75},
                             {"speaker": "SPEAKER_1", "start": 17.28, "end": 21.26}]}

    def diarize_args(self, tmp, minutes=2):
        args = self.args(tmp)
        args.operation, args.file, args.minutes = "diarize", str(Path(tmp) / "meeting.mp3"), minutes
        args.output = str(Path(tmp) / "diarization.json")
        Path(args.file).write_bytes(b"ID3 audio fixture")
        return args

    def test_diarize_service_price_id_and_quota_endpoint(self):
        self.assertEqual(client.OPERATIONS["diarize"], ("diarize",))
        self.assertEqual(client.price_id("diarize", "diarize"), "audio:diarize")
        self.assertEqual(client.QUOTAS["diarize"], "/audio/diarize/quota")
        self.assertIn("diarize", client.SYNCHRONOUS)
        self.assertEqual(client.MAX_DIARIZE_MINUTES, 240)

    def test_diarize_guard_counts_limit_minus_used_minutes(self):
        price = {"prices": [{"model": "audio:diarize", "premium": False}]}
        self.assertIsNone(client.guard("diarize", "diarize", limits(), price, self.diarize_quota(day_used=290), 10))
        for quota_value in (self.diarize_quota(day_used=290), self.diarize_quota(month_used=2995)):
            with self.subTest(quota=quota_value), self.assertRaises(client.Blocked) as raised:
                client.guard("diarize", "diarize", limits(), price, quota_value, 11)
            self.assertIn("Insufficient", str(raised.exception))
        with self.assertRaises(client.Blocked):
            client.guard("diarize", "diarize", limits(), {"prices": [{"model": "audio:diarize", "premium": True}]},
                         self.diarize_quota(), 1)

    def test_diarize_quota_shape_drift_blocks(self):
        price = {"prices": [{"model": "audio:diarize", "premium": False}]}
        good = self.diarize_quota()
        drifted = [{}, dict(good, unit="second"), dict(good, tier="pro"), {k: v for k, v in good.items() if k != "month"},
                   dict(good, day={"limit": 300}), dict(good, day={"used": True, "limit": 300}),
                   dict(good, day={"used": -1, "limit": 300}), dict(good, month={"used": 0, "limit": "3000"}),
                   dict(good, day={"used": 0, "limit": 300.0}), dict(good, day={"remaining": 300})]
        for quota_value in drifted:
            with self.subTest(quota=quota_value), self.assertRaises(client.Blocked):
                client.guard("diarize", "diarize", limits(), price, quota_value, 1)

    def test_diarize_refuses_more_than_four_hours(self):
        price = {"prices": [{"model": "audio:diarize", "premium": False}]}
        self.assertIsNone(client.guard("diarize", "diarize", limits(), price, self.diarize_quota(), 240))
        with self.assertRaises(client.Blocked) as raised:
            client.guard("diarize", "diarize", limits(), price, self.diarize_quota(), 241)
        self.assertIn("4 hours", str(raised.exception))

    def test_diarize_preflight_keeps_only_its_quota_fields(self):
        price = {"prices": [{"model": "audio:diarize", "premium": False}]}
        with patch.object(client, "request", side_effect=[limits(), price, self.diarize_quota()]) as req:
            observed = client.preflight("diarize", "diarize", "fixture", 3)
        self.assertEqual([c.args[:2] for c in req.call_args_list],
                         [("GET", client.HUB + "/limits"), ("GET", client.PRICES), ("GET", client.HUB + "/audio/diarize/quota")])
        self.assertEqual(observed["endpoint"], client.HUB + "/audio/diarize/quota")
        self.assertEqual(observed["units_required"], 3)
        self.assertEqual(observed["buckets"], {k: self.diarize_quota()[k] for k in ("unit", "day", "month")})

    def test_diarize_run_posts_audio_once_and_saves_validated_json(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "diarize"), \
                patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), \
                patch.object(client, "preflight", return_value={"units_required": 2}) as pre:
            args = self.diarize_args(tmp)
            sent = []

            def send(*a, **kw):  # read the body while it is open, as http.client does
                sent.append(drain(a[3]))
                return self.diarization()

            with patch.object(client, "request", side_effect=send) as req:
                result = client.execute(args)
            self.assertEqual(req.call_count, 1)
            method, url, key, _, content_type = req.call_args.args
            body = sent[0]
            self.assertEqual((method, url, key), ("POST", client.HUB + "/audio/diarize", "fixture"))
            self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
            self.assertIn(b'name="file"; filename="upload.mp3"', body)
            self.assertIn(b"ID3 audio fixture", body)
            self.assertEqual(body.count(b"Content-Disposition"), 1)
            self.assertEqual(pre.call_args.args[:4], ("diarize", "diarize", "fixture", 2))
            self.assertEqual(result["status"], "completed")
            self.assertIsNone(result["provider_job_id"])
            self.assertEqual(json.loads(Path(args.output).read_text(encoding="utf-8")), self.diarization())
            self.assertEqual(result["artifacts"][0]["provenance"]["endpoint"], client.HUB + "/audio/diarize")

    def test_diarize_bad_response_requires_reconciliation(self):
        good = self.diarization()
        bad_segment = dict(good["segments"][0], start=6.0)
        cases = [[], {}, dict(good, segments=None), dict(good, segments=[bad_segment]),
                 dict(good, segments=[dict(good["segments"][0], speaker=None)]),
                 dict(good, segments=[dict(good["segments"][0], end=float("nan"))]),
                 dict(good, minutes_charged=True), dict(good, num_speakers=-1), dict(good, duration="61"),
                 dict(good, error="unavailable")]
        for response in cases:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(client, "SERVICE", "diarize"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), \
                    patch.object(client, "preflight", return_value={}):
                args = self.diarize_args(tmp)
                with patch.object(client, "request", return_value=response) as req:
                    result = client.execute(args)
                # The minutes may have been charged: never report it as retryable.
                self.assertEqual(result["status"], "reconciliation_required")
                self.assertEqual(req.call_count, 1)
                self.assertEqual(result["artifacts"], [])
                self.assertFalse(Path(args.output).exists())
                self.assertIn("diarization", result["errors"][0].lower())

    def test_diarize_run_blocks_bad_input_before_any_request(self):
        cases = {"no file": dict(file=None), "payload": dict(payload="extra.json"), "too large": dict()}
        for case, changes in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(client, "SERVICE", "diarize"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), \
                    patch.dict(client.UPLOAD_LIMITS, {"diarize": 4}):
                args = self.diarize_args(tmp)
                if case == "payload":
                    Path(tmp, "extra.json").write_text('{"min_duration": 0.3}', encoding="utf-8")
                    changes = dict(payload=str(Path(tmp, "extra.json")))
                    args.file = str(Path(tmp) / "tiny.mp3")
                    Path(args.file).write_bytes(b"ab")
                for name, value in changes.items():
                    setattr(args, name, value)
                with patch.object(client, "request") as req:
                    result = client.execute(args)
                self.assertEqual(result["status"], "blocked")
                req.assert_not_called()
                self.assertFalse(Path(args.output).exists())

    def test_diarize_is_synchronous_and_refuses_resume(self):
        import io
        import sys
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "diarize"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
            state = Path(tmp) / "state.json"
            argv = ["client.py", "resume", "--job-id", "job-fixture", "--state", str(state), "--output", str(Path(tmp) / "result.json")]
            with patch.object(client, "request") as req, patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(client.main(), 1)
            result = json.loads(out.getvalue())
            self.assertIn("Synchronous diarize has no resumable job endpoint", result["errors"][0])
            self.assertFalse(state.exists())
            req.assert_not_called()

    def test_cli_minutes_flag_reaches_the_guard(self):
        import io
        import sys
        price = {"prices": [{"model": "audio:diarize", "premium": False}]}
        for minutes, expected, code in (("7", "ready", 0), ("0", "blocked", 1)):
            with self.subTest(minutes=minutes), patch.object(client, "SERVICE", "diarize"), \
                    patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), \
                    patch.object(client, "request", side_effect=[limits(), price, self.diarize_quota()]), \
                    patch.object(sys, "argv", ["client.py", "check", "--minutes", minutes]), patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(client.main(), code)
            result = json.loads(out.getvalue())
            self.assertEqual(result["status"], expected)
            if expected == "ready":
                self.assertEqual(result["quota_observed"]["units_required"], 7)

    def test_minutes_flag_and_merge_are_diarization_only(self):
        import io
        import sys
        cases = [("search", ["check", "web", "--minutes", "3"]), ("ocr", ["check", "--minutes", "3"]),
                 ("ocr", ["merge", "--diarization", "d.json", "--transcript", "t.json"]),
                 ("diarize", ["merge", "--diarization", "d.json"]),
                 ("diarize", ["check", "--transcript", "t.json"])]
        for service, argv in cases:
            with self.subTest(service=service, argv=argv), patch.object(client, "SERVICE", service), \
                    patch.object(client, "request") as req, patch.object(client, "resolve_keys") as key, \
                    patch.object(sys, "argv", ["client.py", *argv]), patch("sys.stdout", new_callable=io.StringIO), \
                    patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as raised:
                    client.main()
            self.assertEqual(raised.exception.code, 2)
            req.assert_not_called()
            key.assert_not_called()

    def transcript_json(self):
        # OpenAI verbose_json style, also the shape of a SpeechCore transcript.
        return {"text": "Hello there. Hi. How are you? Fine.", "segments": [
            {"start": 0.0, "end": 2.0, "text": " Hello there."},
            {"start": 2.5, "end": 5.0, "text": " Hi."},
            {"start": 17.0, "end": 19.0, "text": " How are you?"},
            {"start": 19.0, "end": 21.0, "text": " Fine."},
            {"start": 30.0, "end": 31.0, "text": " Bye."}]}

    def test_merge_assigns_each_segment_to_the_speaker_with_most_overlap(self):
        merged = client.merge_transcript(self.diarization(), self.transcript_json())
        self.assertEqual(merged["speakers"], ["SPEAKER_0", "SPEAKER_1"])
        self.assertEqual(merged["turns"], [
            {"speaker": "SPEAKER_0", "start": 0.0, "end": 5.0, "text": "Hello there. Hi."},
            {"speaker": "SPEAKER_1", "start": 17.0, "end": 21.0, "text": "How are you? Fine."},
            {"speaker": None, "start": 30.0, "end": 31.0, "text": "Bye."}])
        self.assertEqual(merged["unassigned_segments"], 1)

    def test_merge_breaks_overlap_ties_by_first_appearance(self):
        diarization = dict(self.diarization(), segments=[
            {"speaker": "SPEAKER_0", "start": 0.0, "end": 1.0}, {"speaker": "SPEAKER_1", "start": 1.0, "end": 2.0},
            {"speaker": "SPEAKER_1", "start": 3.0, "end": 4.0}])
        transcript = {"segments": [{"start": 0.0, "end": 2.0, "text": "tie"},
                                   {"start": 0.5, "end": 4.0, "text": "more"}, {"start": 5, "end": 5, "text": "  "}]}
        merged = client.merge_transcript(diarization, transcript)
        self.assertEqual([(t["speaker"], t["text"]) for t in merged["turns"]], [("SPEAKER_0", "tie"), ("SPEAKER_1", "more")])
        self.assertEqual(merged["unassigned_segments"], 0)

    def test_merge_rejects_invalid_inputs(self):
        good_t, good_d = self.transcript_json(), self.diarization()
        cases = [(good_d, {}), (good_d, {"segments": [{"start": 1, "end": 0, "text": "x"}]}),
                 (good_d, {"segments": [{"start": 0, "end": 1}]}), (good_d, []),
                 ({}, good_t), (dict(good_d, segments=[{"speaker": 1, "start": 0, "end": 1}]), good_t),
                 (dict(good_d, minutes_charged=None), good_t)]
        for diarization, transcript in cases:
            with self.subTest(diarization=diarization, transcript=transcript), self.assertRaises(client.Blocked):
                client.merge_transcript(diarization, transcript)

    def merge_cli(self, tmp, diarization, transcript, output=None, extra=()):
        import io
        import sys
        d, t = Path(tmp) / "diarization.json", Path(tmp) / "transcript.json"
        d.write_text(json.dumps(diarization), encoding="utf-8")
        output = output or str(Path(tmp) / "speakers.json")
        argv = ["client.py", "merge", "--diarization", str(d), "--output", output, *extra]
        if transcript is not None:
            t.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
            argv += ["--transcript", str(t)]
        with patch.object(client, "SERVICE", "diarize"), patch.object(client, "request") as req, \
                patch.object(client, "resolve_keys") as key, patch.object(sys, "argv", argv), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            code = client.main()
        req.assert_not_called()
        key.assert_not_called()
        return code, json.loads(out.getvalue()), Path(output)

    def turns(self, *turns):
        return [dict(zip(("speaker", "start", "end", "text"), t)) for t in turns]

    def test_label_segments_keeps_each_transcript_segment(self):
        _, labeled = client.label_segments(self.diarization(), self.transcript_json())
        self.assertEqual([(c["speaker"], c["start"], c["end"], c["text"]) for c in labeled],
                         [("SPEAKER_0", 0.0, 2.0, "Hello there."), ("SPEAKER_0", 2.5, 5.0, "Hi."),
                          ("SPEAKER_1", 17.0, 19.0, "How are you?"), ("SPEAKER_1", 19.0, 21.0, "Fine."),
                          (None, 30.0, 31.0, "Bye.")])

    def test_webvtt_marks_speakers_with_voice_tags(self):
        _, labeled = client.label_segments(self.diarization(), self.transcript_json())
        self.assertEqual(client.to_webvtt(labeled),
                         "WEBVTT\n\n"
                         "00:00:00.000 --> 00:00:02.000\n<v SPEAKER_0>Hello there.\n\n"
                         "00:00:02.500 --> 00:00:05.000\n<v SPEAKER_0>Hi.\n\n"
                         "00:00:17.000 --> 00:00:19.000\n<v SPEAKER_1>How are you?\n\n"
                         "00:00:19.000 --> 00:00:21.000\n<v SPEAKER_1>Fine.\n\n"
                         "00:00:30.000 --> 00:00:31.000\nBye.\n")

    def test_srt_prefixes_the_speaker_and_numbers_cues(self):
        _, labeled = client.label_segments(self.diarization(), self.transcript_json())
        self.assertEqual(client.to_srt(labeled),
                         "1\n00:00:00,000 --> 00:00:02,000\nSPEAKER_0: Hello there.\n\n"
                         "2\n00:00:02,500 --> 00:00:05,000\nSPEAKER_0: Hi.\n\n"
                         "3\n00:00:17,000 --> 00:00:19,000\nSPEAKER_1: How are you?\n\n"
                         "4\n00:00:19,000 --> 00:00:21,000\nSPEAKER_1: Fine.\n\n"
                         "5\n00:00:30,000 --> 00:00:31,000\nBye.\n")

    def test_subtitle_cues_keep_segment_timing_across_pauses(self):
        diarization = dict(self.diarization(), segments=[{"speaker": "A", "start": 0.0, "end": 12.0}])
        transcript = {"segments": [{"start": 0.0, "end": 2.0, "text": "first"}, {"start": 10.0, "end": 12.0, "text": "second"}]}
        with tempfile.TemporaryDirectory() as tmp:
            _, _, out = self.merge_cli(tmp, diarization, transcript, str(Path(tmp) / "out.vtt"), ["--format", "vtt"])
            self.assertEqual(out.read_text(encoding="utf-8"),
                             "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n<v A>first\n\n00:00:10.000 --> 00:00:12.000\n<v A>second\n")

    def test_subtitles_escape_markup_flatten_lines_and_keep_cues_valid(self):
        merged = self.turns(("A>B", 2.0, 2.0, "a < b & c --> d\n\nnext"), (None, 3723.4567, 3724, "late"))
        self.assertEqual(client.to_webvtt(merged),
                         "WEBVTT\n\n00:00:02.000 --> 00:00:02.001\n<v A&gt;B>a &lt; b &amp; c --&gt; d next\n\n"
                         "01:02:03.457 --> 01:02:04.000\nlate\n")
        self.assertEqual(client.to_srt(merged),
                         "1\n00:00:02,000 --> 00:00:02,001\nA>B: a < b & c --> d next\n\n"
                         "2\n01:02:03,457 --> 01:02:04,000\nlate\n")

    def test_rttm_lists_one_speaker_line_per_segment(self):
        diarization = self.diarization()
        diarization["segments"].append({"speaker": "SPEAKER 2", "start": 30, "end": 30.5})
        self.assertEqual(client.to_rttm(diarization, "meeting"),
                         "SPEAKER meeting 1 1.100 4.650 <NA> <NA> SPEAKER_0 <NA> <NA>\n"
                         "SPEAKER meeting 1 17.280 3.980 <NA> <NA> SPEAKER_1 <NA> <NA>\n"
                         "SPEAKER meeting 1 30.000 0.500 <NA> <NA> SPEAKER_2 <NA> <NA>\n")

    def test_merge_cli_writes_each_format_with_source_hashes(self):
        expected = {"vtt": "WEBVTT\n\n00:00:00.000", "srt": "1\n00:00:00,000", "rttm": "SPEAKER diarization 1 1.100"}
        for fmt, start in expected.items():
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as tmp:
                transcript = None if fmt == "rttm" else self.transcript_json()
                output = str(Path(tmp) / f"out.{fmt}")
                code, result, out = self.merge_cli(tmp, self.diarization(), transcript, output, ["--format", fmt])
                self.assertEqual((code, result["status"]), (0, "completed"))
                self.assertTrue(out.read_text(encoding="utf-8").startswith(start))
                sources = result["artifacts"][0]["provenance"]["sources"]
                self.assertEqual(sources["diarization"], client.hashlib.sha256(Path(tmp, "diarization.json").read_bytes()).hexdigest())
                self.assertEqual("transcript" in sources, fmt != "rttm")

    def test_rttm_file_id_comes_from_the_diarization_file_name(self):
        import io
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            d, out = Path(tmp) / "my meeting.json", Path(tmp) / "out.rttm"
            d.write_text(json.dumps(self.diarization()), encoding="utf-8")
            argv = ["client.py", "merge", "--diarization", str(d), "--format", "rttm", "--output", str(out)]
            with patch.object(client, "SERVICE", "diarize"), patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(client.main(), 0)
            self.assertTrue(out.read_text(encoding="utf-8").startswith("SPEAKER my_meeting 1 1.100 4.650 "))

    def test_format_flag_rules(self):
        import io
        import sys
        cases = [("diarize", ["check", "--format", "vtt"]), ("search", ["check", "web", "--format", "json"]),
                 ("diarize", ["merge", "--diarization", "d.json", "--format", "vtt"]),
                 ("diarize", ["merge", "--diarization", "d.json", "--transcript", "t.json", "--format", "rttm"]),
                 ("diarize", ["merge", "--diarization", "d.json", "--transcript", "t.json", "--format", "stm"])]
        for service, argv in cases:
            with self.subTest(argv=argv), patch.object(client, "SERVICE", service), patch.object(client, "request") as req, \
                    patch.object(sys, "argv", ["client.py", *argv]), patch("sys.stdout", new_callable=io.StringIO), \
                    patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as raised:
                    client.main()
            self.assertEqual(raised.exception.code, 2)
            req.assert_not_called()

    # Cross-review fixes (2026-10-18).
    def test_diarize_post_waits_longer_than_the_default_timeout(self):
        for minutes, seconds in ((3, 120), (10, 300), (240, 7200)):
            with self.subTest(minutes=minutes), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(client, "SERVICE", "diarize"), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), \
                    patch.object(client, "preflight", return_value={}):
                args = self.diarize_args(tmp, minutes=minutes)
                with patch.object(client, "request", return_value=self.diarization()) as req:
                    result = client.execute(args)
                self.assertEqual(req.call_args.kwargs.get("timeout"), seconds)
                self.assertEqual(result["timeouts"]["request_seconds"], seconds)
        self.assertEqual(client.diarize_timeout(1), 120)

    def test_run_checks_the_output_path_before_any_request(self):
        cases = ["output is the input", "missing directory", "existing directory", "current directory"]
        if os.geteuid() != 0:  # root ignores directory permissions
            cases += ["read-only directory", "link in a read-only directory", "unsearchable directory", "unreadable directory"]
        for case in cases:
            for service in ("diarize", "ocr"):
                with self.subTest(case=case, service=service), tempfile.TemporaryDirectory() as tmp, \
                        patch.object(client, "SERVICE", service), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
                    args = self.diarize_args(tmp)
                    args.operation = client.OPERATIONS[service][0]
                    locked = Path(tmp) / "locked"
                    locked.mkdir()
                    args.output = {"output is the input": args.file,
                                   "missing directory": str(Path(tmp) / "missing" / "out.json"),
                                   "existing directory": tmp, "current directory": ".",
                                   "read-only directory": str(locked / "out.json"),
                                   "link in a read-only directory": str(locked / "link.json"),
                                   "unsearchable directory": str(locked / "out.json"),
                                   "unreadable directory": str(locked / "out.json")}[case]
                    if case == "link in a read-only directory":
                        (locked / "link.json").symlink_to(Path(tmp) / "target.json")
                    locked.chmod({"unsearchable directory": 0o600, "unreadable directory": 0o300}.get(case, 0o500))
                    try:
                        with patch.object(client, "request") as req, self.assertRaises(client.Blocked):
                            client.execute(args)
                    finally:
                        locked.chmod(0o700)
                    req.assert_not_called()
                    self.assertEqual(Path(args.file).read_bytes(), b"ID3 audio fixture")
                    self.assertFalse(Path(args.state).exists())

    def test_subtitle_speaker_labels_are_flattened_and_empty_labels_dropped(self):
        merged = self.turns(("A\n\n00:00:09.000 --> 00:00:10.000", 0, 1, "one"), ("  ", 2, 3, "two"))
        self.assertEqual(client.to_webvtt(merged),
                         "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n<v A 00:00:09.000 --&gt; 00:00:10.000>one\n\n"
                         "00:00:02.000 --> 00:00:03.000\ntwo\n")
        self.assertEqual(client.to_srt(merged),
                         "1\n00:00:00,000 --> 00:00:01,000\nA 00:00:09.000 --> 00:00:10.000: one\n\n"
                         "2\n00:00:02,000 --> 00:00:03,000\ntwo\n")

    def test_merge_reports_bad_input_files_clearly(self):
        cases = {"malformed": b'{"segments": [', "nested": b"[" * 100000, "not utf-8": b'{"segments": "\xff"}'}
        for case, data in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                code, result, output = self.merge_cli(tmp, self.diarization(), self.transcript_json())
                Path(tmp, "transcript.json").write_bytes(data)
                code, result, output = self.merge_cli(tmp, self.diarization(), None, extra=["--transcript", str(Path(tmp, "transcript.json"))])
                self.assertEqual((code, result["status"]), (1, "blocked"))
                self.assertEqual(result["errors"], ["Transcript file is not valid UTF-8 JSON"])

    def test_merge_accepts_a_utf8_byte_order_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "transcript.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(self.transcript_json()).encode("utf-8"))
            code, result, _ = self.merge_cli(tmp, self.diarization(), None, extra=["--transcript", str(Path(tmp, "transcript.json"))])
        self.assertEqual((code, result["status"]), (0, "completed"))

    def test_subtitles_block_timestamps_too_large_to_write(self):
        diarization = dict(self.diarization(), segments=[{"speaker": "A", "start": 0.0, "end": 1e308}])
        transcript = {"segments": [{"start": 0.0, "end": 1e308, "text": "x"}]}
        for fmt, expected in (("vtt", "blocked"), ("srt", "blocked"), ("json", "completed")):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as tmp:
                code, result, _ = self.merge_cli(tmp, diarization, transcript, str(Path(tmp) / f"out.{fmt}"), ["--format", fmt])
                self.assertEqual(result["status"], expected)
                if expected == "blocked":
                    self.assertIn("too large", result["errors"][0])

    def test_minimum_cue_length_is_applied_after_rounding(self):
        merged = self.turns(("S", 0.0015, 0.0015, "x"))
        self.assertIn("00:00:00.002 --> 00:00:00.003", client.to_webvtt(merged))
        self.assertIn("00:00:00,002 --> 00:00:00,003", client.to_srt(merged))

    def test_huge_numbers_are_invalid_not_a_crash(self):
        self.assertFalse(client.finite(10 ** 400))
        with self.assertRaises(client.Blocked):
            client.merge_transcript(dict(self.diarization(), duration=10 ** 400), self.transcript_json())
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "diarize"), \
                patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), patch.object(client, "preflight", return_value={}):
            args = self.diarize_args(tmp)
            with patch.object(client, "request", return_value=dict(self.diarization(), duration=10 ** 400)):
                result = client.execute(args)
            self.assertEqual(result["status"], "reconciliation_required")
            self.assertIn("diarization", result["errors"][0].lower())

    def test_merge_orders_segments_by_time(self):
        diarization = dict(self.diarization(), segments=[
            {"speaker": "B", "start": 1.0, "end": 2.0}, {"speaker": "A", "start": 0.0, "end": 1.0},
            {"speaker": "A", "start": 9.0, "end": 13.0}])
        transcript = {"segments": [{"start": 10.0, "end": 12.0, "text": "later"},
                                   {"start": 0.0, "end": 2.0, "text": "tie"}]}
        merged = client.merge_transcript(diarization, transcript)
        self.assertEqual(merged["speakers"], ["A", "B"])
        self.assertEqual([(t["speaker"], t["start"], t["text"]) for t in merged["turns"]], [("A", 0.0, "tie later")])

    def test_equal_overlaps_tie_despite_float_rounding(self):
        diarization = dict(self.diarization(), segments=[
            {"speaker": "A", "start": 0.1, "end": 0.2}, {"speaker": "B", "start": 0.3, "end": 0.4}])
        merged = client.merge_transcript(diarization, {"segments": [{"start": 0.0, "end": 0.5, "text": "x"}]})
        self.assertEqual(merged["turns"][0]["speaker"], "A")

    def test_diarize_file_cap_is_500_mb_and_inclusive(self):
        # Only the limits a provider documents: diarization's 500 MB.
        self.assertEqual(client.UPLOAD_LIMITS, {"diarize": 500 * 1000 * 1000})
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "diarize"), patch.dict(client.UPLOAD_LIMITS, {"diarize": 4}):
            args = self.diarize_args(tmp)
            for size, allowed in ((4, True), (5, False)):
                Path(args.file).write_bytes(b"x" * size)
                with self.subTest(size=size):
                    if allowed:
                        method, _, body, _ = client.submission(args)
                        body.close()  # the upload file is open until it is sent
                        self.assertEqual(method, "POST")
                    else:
                        with self.assertRaises(client.Blocked):
                            client.submission(args)
            with patch.object(client, "SERVICE", "ocr"):
                Path(args.file).write_bytes(b"x" * 50)
                method, _, body, _ = client.submission(args)
                body.close()
                self.assertEqual(method, "POST")

    # Fourth cross-review (2026-10-18).
    def test_run_blocks_an_output_another_user_owns_in_a_sticky_directory(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "diarize"), \
                patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
            sticky = Path(tmp) / "shared"
            sticky.mkdir()
            sticky.chmod(0o1777)
            args = self.diarize_args(tmp)
            args.output = str(sticky / "out.json")
            Path(args.output).write_text("{}", encoding="utf-8")
            # Pretend to be a user who owns neither the file nor the directory.
            with patch.object(client.os, "geteuid", return_value=os.geteuid() + 4242), \
                    patch.object(client, "request") as req, self.assertRaises(client.Blocked) as raised:
                client.execute(args)
            req.assert_not_called()
            self.assertIn("sticky", str(raised.exception))
            # The real owner, or a new file, is fine.
            for output in (args.output, str(sticky / "new.json")):
                args.output, args.state = output, str(Path(tmp) / f"state-{Path(output).stem}.json")
                with patch.object(client, "preflight", return_value={}), \
                        patch.object(client, "request", return_value=self.diarization()):
                    self.assertEqual(client.execute(args)["status"], "completed")
            # Without the sticky bit, a writable directory lets anyone replace the file.
            plain = Path(tmp) / "plain"
            plain.mkdir()
            args.output, args.state = str(plain / "out.json"), str(Path(tmp) / "state-plain.json")
            Path(args.output).write_text("{}", encoding="utf-8")
            with patch.object(client.os, "geteuid", return_value=os.geteuid() + 4242), \
                    patch.object(client, "preflight", return_value={}), \
                    patch.object(client, "request", return_value=self.diarization()):
                self.assertEqual(client.execute(args)["status"], "completed")

    def test_rttm_file_id_survives_a_non_utf8_file_name(self):
        import io
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / os.fsdecode(b"caf\xe9 talk.json")
            d.write_text(json.dumps(self.diarization()), encoding="utf-8")
            out = Path(tmp) / "out.rttm"
            argv = ["client.py", "merge", "--diarization", str(d), "--format", "rttm", "--output", str(out)]
            with patch.object(client, "SERVICE", "diarize"), patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(client.main(), 0)
            self.assertTrue(out.read_text(encoding="utf-8").startswith("SPEAKER caf\ufffd_talk 1 1.100 "))

    def test_ties_follow_who_spoke_first_not_the_name(self):
        diarization = dict(self.diarization(), segments=[
            {"speaker": "B", "start": 0.0, "end": 1.0}, {"speaker": "A", "start": 1.0, "end": 2.0}])
        merged = client.merge_transcript(diarization, {"segments": [{"start": 0.0, "end": 2.0, "text": "x"}]})
        self.assertEqual(merged["speakers"], ["B", "A"])
        self.assertEqual(merged["turns"][0]["speaker"], "B")

    # Fifth cross-review (2026-10-18).
    def test_run_blocks_an_output_name_the_system_refuses(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "diarize"), \
                patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
            args = self.diarize_args(tmp)
            args.output = str(Path(tmp) / ("x" * 256 + ".json"))
            with patch.object(client, "request") as req, self.assertRaises(client.Blocked) as raised:
                client.execute(args)
            req.assert_not_called()
            self.assertIn("not usable", str(raised.exception))

    def test_aliases_of_an_input_are_refused_as_output(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "diarize"), \
                patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
            args = self.diarize_args(tmp)
            alias = Path(tmp) / "alias.mp3"
            os.link(args.file, alias)  # a hard link resolves to a different path
            args.output = str(alias)
            with patch.object(client, "request") as req, self.assertRaises(client.Blocked):
                client.execute(args)
            req.assert_not_called()
            self.assertEqual(Path(args.file).read_bytes(), b"ID3 audio fixture")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "transcript.json").write_text(json.dumps(self.transcript_json()), encoding="utf-8")
            os.link(Path(tmp, "transcript.json"), Path(tmp, "alias.json"))
            code, result, _ = self.merge_cli(tmp, self.diarization(), self.transcript_json(), output=str(Path(tmp, "alias.json")))
            self.assertEqual((code, result["status"]), (1, "blocked"))
            self.assertEqual(json.loads(Path(tmp, "transcript.json").read_text(encoding="utf-8")), self.transcript_json())

    def test_rttm_blocks_invalid_input_with_specific_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, result, _ = self.merge_cli(tmp, {"segments": "x"}, None, extra=["--format", "rttm"])
            self.assertEqual(result["errors"], ["Diarization input does not match the /v1/audio/diarize JSON shape"])
            lone = Path(tmp) / "lone.json"
            lone.write_text('{"duration": 2.0, "num_speakers": 1, "minutes_charged": 1, '
                            '"segments": [{"speaker": "\\ud83d", "start": 0, "end": 1}]}', encoding="utf-8")
            argv_extra = ["--format", "rttm"]
            import io
            import sys
            argv = ["client.py", "merge", "--diarization", str(lone), "--output", str(Path(tmp) / "out.rttm"), *argv_extra]
            with patch.object(client, "SERVICE", "diarize"), patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(client.main(), 1)
            self.assertEqual(json.loads(out.getvalue())["errors"], ["Input text is not valid Unicode"])

    def test_a_valid_answer_with_a_lone_surrogate_is_still_saved(self):
        answer = dict(self.diarization(), segments=[{"speaker": "\ud83d", "start": 1.0, "end": 2.0}])
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "diarize"), \
                patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), patch.object(client, "preflight", return_value={}):
            args = self.diarize_args(tmp)
            with patch.object(client, "request", return_value=answer):
                result = client.execute(args)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(json.loads(Path(args.output).read_text(encoding="utf-8")), answer)

    # Sixth cross-review (2026-10-18).
    def test_state_and_output_that_are_one_file_are_refused(self):
        real_samefile = os.path.samefile

        def case_insensitive(a, b):  # simulate macOS or Windows
            pa, pb = Path(a), Path(b)
            # Both names are one file there as soon as either exists.
            if pa.parent == pb.parent and pa.name.lower() == pb.name.lower() and (pa.exists() or pb.exists()):
                return True
            return real_samefile(a, b)

        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "diarize"), \
                patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), \
                patch.object(client.os.path, "samefile", side_effect=case_insensitive):
            args = self.diarize_args(tmp)
            args.state, args.output = str(Path(tmp) / "Diarization.json"), str(Path(tmp) / "diarization.json")
            with patch.object(client, "preflight") as pre, patch.object(client, "request") as req:
                result = client.execute(args)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("State and artifact paths must differ", result["errors"][0])
            pre.assert_not_called()
            req.assert_not_called()

    def test_run_probes_that_the_output_name_can_be_created(self):
        real_open = os.open
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "diarize"), \
                patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
            args = self.diarize_args(tmp)
            args.output = str(Path(tmp) / "meeting:1.json")

            def fat_open(path, flags, *rest):  # vfat refuses ':' on create, not on lookup
                if str(path) == args.output:
                    raise OSError(22, "Invalid argument")
                return real_open(path, flags, *rest)

            with patch.object(client.os, "open", side_effect=fat_open), patch.object(client, "request") as req, \
                    self.assertRaises(client.Blocked) as raised:
                client.execute(args)
            req.assert_not_called()
            self.assertIn("not usable", str(raised.exception))
            # A usable name is probed and removed again before the preflight.
            args.output, args.state = str(Path(tmp) / "fine.json"), str(Path(tmp) / "state2.json")
            with patch.object(client, "request", side_effect=client.RequestError("HTTP 503")):
                self.assertEqual(client.execute(args)["status"], "blocked")
            self.assertFalse(Path(args.output).exists())

    def test_unassigned_segments_stay_separate_turns(self):
        transcript = {"segments": [{"start": 10.0, "end": 12.0, "text": "one"}, {"start": 40.0, "end": 41.0, "text": "two"}]}
        merged = client.merge_transcript(self.diarization(), transcript)
        self.assertEqual([(t["speaker"], t["start"], t["end"], t["text"]) for t in merged["turns"]],
                         [(None, 10.0, 12.0, "one"), (None, 40.0, 41.0, "two")])
        self.assertEqual(merged["unassigned_segments"], 2)

    # Seventh cross-review (2026-10-18).
    def test_input_file_that_is_the_state_file_is_never_sent(self):
        for service, via in (("diarize", "same path"), ("ocr", "same path"), ("diarize", "symlink")):
            with self.subTest(service=service, via=via), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(client, "SERVICE", service), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
                args = self.diarize_args(tmp)
                args.operation = client.OPERATIONS[service][0]
                args.file = args.state = str(Path(tmp) / "x.json")  # does not exist yet
                if via == "symlink":  # a dangling link to the future state file
                    args.file = str(Path(tmp) / "link.mp3")
                    os.symlink(args.state, args.file)
                with patch.object(client, "preflight") as pre, patch.object(client, "request") as req, \
                        patch.object(client, "multipart") as body:
                    result = client.execute(args)
                self.assertEqual(result["status"], "blocked")
                self.assertIn("Input file must differ from the state file", result["errors"][0])
                pre.assert_not_called()
                req.assert_not_called()
                body.assert_not_called()

    # Eighth cross-review (2026-10-18).
    def test_rttm_never_merges_speakers_with_broken_labels(self):
        import io
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "broken.json"
            d.write_text('{"duration": 3.0, "num_speakers": 2, "minutes_charged": 1, "segments": ['
                         '{"speaker": "\\udce9", "start": 0, "end": 1}, {"speaker": "\\udcea", "start": 1, "end": 2}]}',
                         encoding="utf-8")
            argv = ["client.py", "merge", "--diarization", str(d), "--format", "rttm", "--output", str(Path(tmp) / "out.rttm")]
            with patch.object(client, "SERVICE", "diarize"), patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(client.main(), 1)
            self.assertEqual(json.loads(out.getvalue())["errors"], ["Input text is not valid Unicode"])
            self.assertFalse((Path(tmp) / "out.rttm").exists())

    # Ninth cross-review (2026-10-18): streamed uploads.
    def test_upload_body_streams_the_file_in_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.wav"
            audio.write_bytes(bytes(range(256)) * 800)  # 204800 bytes
            with patch.object(client.Path, "read_bytes", side_effect=AssertionError("loaded whole file")):
                body, content_type = client.multipart(audio, "file", {})
                blocks = list(iter(lambda: body.read(8192), b""))
            boundary = content_type.split("; boundary=")[1].encode()
            data = b"".join(blocks)
            self.assertEqual(len(data), len(body))
            self.assertTrue(data.startswith(b"--" + boundary + b"\r\n"))
            self.assertIn(b"\r\n\r\n" + audio.read_bytes() + b"\r\n--" + boundary + b"--\r\n", data)
            self.assertTrue(all(len(b) <= 8192 for b in blocks[1:-1]))
            self.assertIsNone(body.file)  # closed after the last block

    def test_upload_body_sends_exactly_the_checked_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.wav"
            audio.write_bytes(b"x" * 10000)
            body, _ = client.multipart(audio, "file", {})
            with audio.open("ab") as grow:
                grow.write(b"y" * 5000)
            data = drain(body)
            self.assertEqual(len(data), len(body))
            self.assertNotIn(b"yy", data)
            self.assertEqual(data.count(b"x"), 10000)
            body, _ = client.multipart(audio, "file", {})
            audio.write_bytes(b"x" * 10)
            with self.assertRaises(OSError):
                drain(body)
            self.assertIsNone(body.file)

    def test_http_client_sends_the_body_block_by_block(self):
        import http.client

        class Sock:
            def __init__(self):
                self.sent, self.first = [], None

            def sendall(self, data):
                self.first = data if self.first is None else self.first
                self.sent.append(len(data))

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.wav"
            audio.write_bytes(b"z" * 100000)
            body, content_type = client.multipart(audio, "file", {})
            conn = http.client.HTTPSConnection("api.neuraldeep.ru")
            conn.sock = Sock()
            conn.request("POST", "/v1/audio/diarize", body=body,
                         headers={"Content-Type": content_type, "Content-Length": str(len(body))})
            # Each sendall is one socket-timeout window: no single send carries the whole file.
            self.assertGreater(len(conn.sock.sent), 10)
            self.assertLessEqual(max(conn.sock.sent[1:]), 8192)
            self.assertIn(b"Content-Length: %d\r\n" % len(body), self.first_or(conn.sock))
            self.assertNotIn(b"Transfer-Encoding", self.first_or(conn.sock))

    def first_or(self, sock):
        return bytes(sock.first)

    def test_request_streams_a_body_with_its_length_and_closes_it(self):
        from unittest.mock import MagicMock  # not every sibling suite imports it
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.wav"
            audio.write_bytes(b"q" * 30000)
            for outcome in ("ok", "error"):
                with self.subTest(outcome=outcome):
                    body, content_type = client.multipart(audio, "file", {})
                    body.read(10)  # start streaming so a file handle is open
                    body.read(10)
                    opener = MagicMock()
                    if outcome == "ok":
                        response = opener.open.return_value.__enter__.return_value
                        response.read.return_value = b"{}"
                        response.headers.get.return_value = "application/json"
                        client.request("POST", client.HUB + "/audio/diarize", "fixture", body, content_type, opener=opener)
                    else:
                        opener.open.side_effect = TimeoutError()
                        with self.assertRaises(client.RequestError):
                            client.request("POST", client.HUB + "/audio/diarize", "fixture", body, content_type, opener=opener)
                    sent = opener.open.call_args.args[0]
                    self.assertIs(sent.data, body)
                    self.assertEqual(sent.get_header("Content-length"), str(len(body)))
                    self.assertIsNone(sent.get_header("Transfer-encoding"))
                    self.assertIsNone(body.file)

    # Tenth cross-review (2026-10-18).
    def test_unreadable_or_irregular_input_blocks_before_any_request(self):
        cases = ["directory", "fifo"] + (["unreadable"] if os.geteuid() != 0 else [])
        for case in cases:
            for service in ("diarize", "ocr"):
                with self.subTest(case=case, service=service), tempfile.TemporaryDirectory() as tmp, \
                        patch.object(client, "SERVICE", service), patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
                    args = self.diarize_args(tmp)
                    args.operation = client.OPERATIONS[service][0]
                    if case == "directory":
                        args.file = tmp
                    elif case == "fifo":  # opening a FIFO for reading would wait for a writer
                        args.file = str(Path(tmp) / "pipe")
                        os.mkfifo(args.file)
                    else:
                        Path(args.file).chmod(0)
                    try:
                        with patch.object(client, "preflight") as pre, patch.object(client, "request") as req:
                            result = client.execute(args)
                    finally:
                        if case == "unreadable":
                            Path(args.file).chmod(0o600)
                    self.assertEqual(result["status"], "blocked")
                    self.assertIn("Input file", result["errors"][0])
                    pre.assert_not_called()
                    req.assert_not_called()

    def test_upload_file_is_closed_when_no_request_is_made(self):
        bodies = []
        real = client.multipart

        def keep(*a, **kw):
            bodies.append(real(*a, **kw))
            return bodies[-1]

        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "diarize"), \
                patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), patch.object(client, "multipart", side_effect=keep), \
                patch.object(client, "preflight", side_effect=client.Blocked("Insufficient or invalid service quota")):
            result = client.execute(self.diarize_args(tmp))
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(bodies[0][0].file)

    # Eleventh cross-review (2026-10-18).
    def test_upload_descriptor_is_blocking_after_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.wav"
            audio.write_bytes(b"w" * 100)
            body, _ = client.multipart(audio, "file", {})
            try:
                # O_NONBLOCK only guards the open; some FUSE or network file systems honour it on reads.
                self.assertTrue(os.get_blocking(body.file.fileno()))
            finally:
                body.close()

    # Per-skill keys (2026-10-18): several keys, the first that passes is used.
    def test_check_tries_keys_in_order_and_names_the_one_used(self):
        import io
        import sys
        keys = ("NEURALDEEP_DIARIZATION_API_KEY", ["key-one-secret", "key-two-secret"])
        answers = [client.Blocked("Insufficient or invalid service quota"), {"units_required": 2}]
        with patch.object(client, "SERVICE", "diarize"), patch.object(client, "resolve_keys", return_value=keys), \
                patch.object(client, "preflight", side_effect=answers) as pre, \
                patch.object(sys, "argv", ["client.py", "check", "--minutes", "2"]), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(client.main(), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["quota_observed"]["key"], {"variable": "NEURALDEEP_DIARIZATION_API_KEY", "index": 2})
        self.assertEqual([c.args[2] for c in pre.call_args_list], ["key-one-secret", "key-two-secret"])
        self.assertNotIn("secret", out.getvalue())
        answers = [client.Blocked("Insufficient or invalid service quota"), client.RequestError("HTTP 401; no automatic retry")]
        with patch.object(client, "SERVICE", "diarize"), patch.object(client, "resolve_keys", return_value=keys), \
                patch.object(client, "preflight", side_effect=answers), \
                patch.object(sys, "argv", ["client.py", "check", "--minutes", "2"]), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(client.main(), 1)
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["errors"], ["key 1: Insufficient or invalid service quota; key 2: HTTP 401; no automatic retry"])
        self.assertNotIn("secret", out.getvalue())

    def test_run_posts_with_the_first_key_that_passes_and_records_its_number(self):
        keys = ("NEURALDEEP_DIARIZATION_API_KEY", ["key-one-secret", "key-two-secret"])
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "diarize"), \
                patch.object(client, "resolve_keys", return_value=keys), \
                patch.object(client, "preflight", side_effect=[client.Blocked("Insufficient or invalid service quota"),
                                                               {"units_required": 2}]):
            args = self.diarize_args(tmp)
            with patch.object(client, "request", return_value=self.diarization()) as req:
                result = client.execute(args)
            saved = Path(args.state).read_text(encoding="utf-8")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(req.call_args.args[2], "key-two-secret")
        self.assertEqual(json.loads(saved)["quota_observed"]["key"], {"variable": "NEURALDEEP_DIARIZATION_API_KEY", "index": 2})
        self.assertNotIn("secret", saved)

    def test_resume_uses_the_key_that_submitted_the_job(self):
        def resume(keys, recorded, blocked=False):
            with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "speechcore"), \
                    patch.object(client, "resolve_keys", return_value=keys):
                args = self.args(tmp)
                args.command, args.operation = "resume", "upload"
                state = client.state_template("task-fixture", "speechcore")
                state.update(provider_job_id="job-fixture", status="timed_out", quota_observed={"key": recorded})
                client.save_state(args.state, state)
                before = Path(args.state).read_bytes()
                with patch.object(client, "request", side_effect=[{"status": "completed"}, (self.transcript(), "application/json")]) as req:
                    if not blocked:
                        return client.execute(args), req
                    with self.assertRaises(client.Blocked) as raised:
                        client.execute(args)
                # The state stays resumable once the key is configured again.
                self.assertEqual(Path(args.state).read_bytes(), before)
                return raised.exception, req
        both = ("NEURALDEEP_SPEECHCORE_API_KEY", ["key-one", "key-two"])
        result, req = resume(both, {"variable": "NEURALDEEP_SPEECHCORE_API_KEY", "index": 2})
        self.assertEqual(result["status"], "completed")
        self.assertEqual({c.args[2] for c in req.call_args_list}, {"key-two"})
        for keys, recorded, reason in (
                (both, {"variable": "NEURALDEEP_SPEECHCORE_API_KEY", "index": 3}, "key 3"),
                (("NEURALDEEP_API_KEY", ["key-one"]), {"variable": "NEURALDEEP_SPEECHCORE_API_KEY", "index": 1},
                 "NEURALDEEP_SPEECHCORE_API_KEY")):
            with self.subTest(recorded=recorded):
                error, req = resume(keys, recorded, blocked=True)
                self.assertIn(reason, str(error))
                req.assert_not_called()
        # A job attached with --job-id, or a state without a key record, uses the first key.
        result, req = resume(both, None)
        self.assertEqual({c.args[2] for c in req.call_args_list}, {"key-one"})

    # API error explanations (2026-10-18).
    def http_error(self, code, headers=None):
        import io
        from urllib.error import HTTPError
        opener = Mock()
        body = io.BytesIO(b'{"detail": "private-body-fixture"}')
        opener.open.side_effect = HTTPError("https://example.com", code, "private-reason", headers or {}, body)
        with self.assertRaises(client.RequestError) as raised:
            client.request("POST", client.HUB + "/audio/diarize", "secret-fixture", b"{}", opener=opener)
        message = str(raised.exception)
        for private in ("private-body-fixture", "private-reason", "secret-fixture"):
            self.assertNotIn(private, message)
        self.assertEqual(opener.open.call_count, 1)
        return message

    def test_http_errors_are_explained_from_a_fixed_table(self):
        self.assertEqual(sorted(client.ERROR_HINTS), [400, 401, 408, 422, 429, 500, 502, 503])
        for code, hint in client.ERROR_HINTS.items():
            with self.subTest(code=code):
                self.assertEqual(self.http_error(code), f"HTTP {code} ({hint}); no automatic retry")
        self.assertEqual(self.http_error(418), "HTTP 418; no automatic retry")

    def test_retry_after_and_window_are_reported_only_when_well_formed(self):
        message = self.http_error(429, {"Retry-After": "30", "X-Window": "week"})
        self.assertEqual(message, f"HTTP 429 ({client.ERROR_HINTS[429]}); retry after 30 s; window week; no automatic retry")
        self.assertIn("retry after 120 s", self.http_error(503, {"Retry-After": " 120 "}))
        for headers in ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, {"Retry-After": "1e9"},
                        {"Retry-After": "9999999"}, {"Retry-After": "-5"}, {"X-Window": "session\nX-Evil: 1"},
                        {"X-Window": "evil"}):
            with self.subTest(headers=headers):
                message = self.http_error(429, headers)
                self.assertEqual(message, f"HTTP 429 ({client.ERROR_HINTS[429]}); no automatic retry")
        self.assertIn("window session", self.http_error(429, {"X-Window": "Session"}))

    def test_skill_documents_every_explained_error_code(self):
        import re
        text = (Path(__file__).parents[1] / "SKILL.md").read_text(encoding="utf-8")
        match = re.search(r"^## Error handling\n(.*?)(?=^## |\Z)", text, re.M | re.S | re.I)
        self.assertIsNotNone(match, "SKILL.md has no Error handling section")
        section = match.group(1)
        for code in client.ERROR_HINTS:
            with self.subTest(code=code):
                self.assertIn(f"| `{code}` |", section)
        for needle in ("Retry-After", "X-Window", "blocked", "reconciliation_required", "poll_error",
                       "in the environment or `~/.coddy/.env`", "never read the file yourself"):
            self.assertIn(needle, section)

    # Twelfth cross-review (2026-10-18).
    def test_a_non_utf8_byte_does_not_hide_the_rest_of_the_env_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODDY_HOME": tmp}, clear=True):
            # Coddy reads .env line by line as bytes, so one cp1251 comment hides nothing.
            Path(tmp, ".env").write_bytes("# ключ для диаризации\n".encode("cp1251") + b"NEURALDEEP_DIARIZATION_API_KEY=sk-fixture\n")
            self.assertEqual(client.resolve_keys("diarize"), ("NEURALDEEP_DIARIZATION_API_KEY", ["sk-fixture"]))

    # Thirteenth cross-review (2026-10-18).
    def test_run_never_replaces_an_output_that_is_not_a_regular_file(self):
        for case in ("fifo", "link to fifo", "link to device"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "diarize"), \
                    patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])):
                args = self.diarize_args(tmp)
                fifo = Path(tmp) / "pipe"
                os.mkfifo(fifo)
                args.output = str(fifo) if case == "fifo" else str(Path(tmp) / "out.json")
                if case != "fifo":
                    os.symlink(fifo if case == "link to fifo" else os.devnull, args.output)
                with patch.object(client, "request") as req, self.assertRaises(client.Blocked) as raised:
                    client.execute(args)
                req.assert_not_called()
                self.assertIn("not a regular file", str(raised.exception))
                self.assertTrue(stat_is_fifo(fifo))
        # A dangling link is replaced by the answer like a missing file.
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "SERVICE", "diarize"), \
                patch.object(client, "resolve_keys", return_value=("NEURALDEEP_API_KEY", ["fixture"])), \
                patch.object(client, "preflight", return_value={}):
            args = self.diarize_args(tmp)
            args.output = str(Path(tmp) / "out.json")
            os.symlink(Path(tmp) / "nowhere.json", args.output)
            with patch.object(client, "request", return_value=self.diarization()):
                self.assertEqual(client.execute(args)["status"], "completed")

    # Wallet billing (2026-10-19): only with the user's approval, as a rouble limit.
    def wallet_limits(self, balance=100.0, status="ok", cap=0.0, spent=0.0, cap_blocked=False):
        return {"tier": "starter", "key": {"status": status, "billing_mode": "wallet",
                                           "cap": {"spend_cap_rub": cap, "spent_rub": spent, "cap_blocked": cap_blocked}},
                "wallet": {"balance_rub": balance, "spent_rub_30d": 0.0}}

    def diarize_prices(self, rate=0.3, premium=False):
        return {"prices": [{"model": "audio:diarize", "billing": "unit", "unit_rub": rate, "premium": premium}]}

    def cli(self, argv, responses, keys=("NEURALDEEP_DIARIZATION_API_KEY", ["wallet-secret"]), service="diarize"):
        import io
        import sys
        with patch.object(client, "SERVICE", service), patch.object(client, "resolve_keys", return_value=keys), \
                patch.object(client, "request", side_effect=responses) as req, \
                patch.object(sys, "argv", ["client.py", *argv]), patch("sys.stdout", new_callable=io.StringIO) as out:
            code = client.main()
        self.assertNotIn("secret", out.getvalue())
        return code, json.loads(out.getvalue()), req

    def test_a_wallet_key_asks_for_approval_with_a_cost_estimate(self):
        code, result, req = self.cli(["check", "--minutes", "2"], [self.wallet_limits(), self.diarize_prices()])
        self.assertEqual((code, result["status"]), (1, "wallet_approval_required"))
        observed = result["quota_observed"]
        for field, value in (("billing", "wallet"), ("price_id", "audio:diarize"), ("units", 2), ("rate_rub", 0.3),
                             ("estimate_rub", 0.6), ("balance_rub", 100.0)):
            self.assertEqual(observed[field], value)
        self.assertEqual(observed["key"], {"variable": "NEURALDEEP_DIARIZATION_API_KEY", "index": 1})
        for needle in ("pays from the wallet", "0.60", "100.00", "Ask the user", "--wallet-limit 0.60"):
            self.assertIn(needle, result["errors"][0])
        # No subscription quota is read for a wallet key.
        self.assertEqual([c.args[1] for c in req.call_args_list], [client.HUB + "/limits", client.PRICES])

    def test_an_approved_wallet_limit_lets_the_run_pay(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.diarize_args(tmp)
            argv = ["run", "diarize", "--file", args.file, "--minutes", "2", "--wallet-limit", "1",
                    "--state", args.state, "--output", args.output]
            code, result, req = self.cli(argv, [self.wallet_limits(), self.diarize_prices(), self.diarization()])
            saved = json.loads(Path(args.state).read_text(encoding="utf-8"))
        self.assertEqual((code, result["status"]), (0, "completed"))
        self.assertEqual(req.call_args_list[-1].args[:2], ("POST", client.HUB + "/audio/diarize"))
        self.assertEqual((saved["quota_observed"]["billing"], saved["quota_observed"]["estimate_rub"],
                          saved["quota_observed"]["approved_limit_rub"]), ("wallet", 0.6, 1.0))
        code, result, req = self.cli(["check", "--minutes", "2", "--wallet-limit", "0.6"],
                                     [self.wallet_limits(), self.diarize_prices()])
        self.assertEqual((code, result["status"]), (0, "ready"))

    def test_wallet_refusals_happen_before_any_billed_request(self):
        cases = {
            "over the approved limit": ([self.wallet_limits(), self.diarize_prices()], "0.5", "exceeds the approved"),
            "balance too low": ([self.wallet_limits(balance=0.5), self.diarize_prices()], "5", "balance"),
            "key spending cap reached": ([self.wallet_limits(cap_blocked=True), self.diarize_prices()], "5", "spending cap"),
            "key spending cap too low": ([self.wallet_limits(cap=1.0, spent=0.9), self.diarize_prices()], "5", "spending cap"),
            "key not ok": ([self.wallet_limits(status="disabled"), self.diarize_prices()], "5", "ok key"),
            "no price row": ([self.wallet_limits(), {"prices": []}], "5", "price"),
            "rate is not a number": ([self.wallet_limits(), self.diarize_prices(rate=True)], "5", "price"),
            "no wallet balance": ([dict(self.wallet_limits(), wallet={}), self.diarize_prices()], "5", "schema"),
        }
        for case, (responses, limit, reason) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                args = self.diarize_args(tmp)
                argv = ["run", "diarize", "--file", args.file, "--minutes", "2", "--wallet-limit", limit,
                        "--state", args.state, "--output", args.output]
                code, result, req = self.cli(argv, responses)
                self.assertEqual((code, result["status"]), (1, "blocked"))
                self.assertIn(reason, result["errors"][0])
                self.assertNotIn("POST", [c.args[0] for c in req.call_args_list])

    def test_subscription_keys_are_preferred_over_wallet_keys(self):
        keys = ("NEURALDEEP_DIARIZATION_API_KEY", ["wallet-secret", "subscription-secret"])
        responses = [self.wallet_limits(), self.diarize_prices(),
                     limits(), self.diarize_prices(), self.diarize_quota()]
        code, result, req = self.cli(["check", "--minutes", "2", "--wallet-limit", "5"], responses, keys)
        self.assertEqual((code, result["status"]), (0, "ready"))
        self.assertEqual(result["quota_observed"]["billing"], "subscription")
        self.assertEqual(result["quota_observed"]["key"]["index"], 2)

    def transcription_answer(self):
        return {"text": "Привет. Hello.", "language": "ru", "duration": 3.0,
                "segments": [{"start": 0.0, "end": 1.5, "text": "Привет."}, {"start": 1.5, "end": 3.0, "text": "Hello."}]}

    def audio_args(self, tmp, command, service_op):
        audio = Path(tmp) / "talk.wav"
        audio.write_bytes(b"RIFF audio fixture")
        return [command, service_op, "--file", str(audio), "--minutes", "3", "--state", str(Path(tmp) / "state.json"),
                "--output", str(Path(tmp) / "result.json")]

    def test_the_wallet_pays_for_transcription_with_approval(self):
        rows = {"prices": [{"model": "whisper-1", "billing": "minute", "rub_per_min": 1.0, "out_rub_1m": 102.0, "premium": False}]}
        keys = ("NEURALDEEP_TRANSCRIPTIONS_API_KEY", ["wallet-secret"])
        code, result, req = self.cli(["check", "--model", "whisper-1", "--minutes", "3"], [self.wallet_limits(), rows], keys, "whisper")
        self.assertEqual((code, result["status"]), (1, "wallet_approval_required"))
        self.assertEqual(result["quota_observed"]["estimate_rub"], 3.0)
        self.assertIn("output tokens", result["errors"][0])  # the row also prices tokens the estimate leaves out
        with tempfile.TemporaryDirectory() as tmp:
            argv = self.audio_args(tmp, "run", "transcribe") + ["--model", "whisper-1", "--wallet-limit", "3"]
            sent = []

            def respond(*a, **kw):
                if a[0] == "POST":
                    sent.append((a[1], drain(a[3]), kw.get("timeout")))
                    return self.transcription_answer()
                return answers.pop(0)
            answers = [self.wallet_limits(), rows]
            with patch.object(client, "SERVICE", "whisper"), patch.object(client, "resolve_keys", return_value=keys), \
                    patch.object(client, "request", side_effect=respond):
                import io
                import sys
                with patch.object(sys, "argv", ["client.py", *argv]), patch("sys.stdout", new_callable=io.StringIO) as out:
                    self.assertEqual(client.main(), 0)
            saved = json.loads(Path(tmp, "result.json").read_text(encoding="utf-8"))
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "completed")
        url, body, timeout = sent[0]
        self.assertEqual((url, timeout), (client.HUB + "/audio/transcriptions", 120))
        self.assertIn(b'name="model"\r\n\r\nwhisper-1\r\n', body)
        self.assertIn(b'name="file"; filename="upload.wav"', body)
        self.assertEqual(saved, self.transcription_answer())

    def test_an_unrecognized_transcription_answer_needs_reconciliation(self):
        rows = {"prices": [{"model": "gigaam-v3", "billing": "minute", "rub_per_min": 0.25, "premium": False}]}
        for answer in ({}, {"text": None}, {"text": "x", "segments": [{"start": 2, "end": 1, "text": "x"}]}, {"text": "x", "error": "busy"}, []):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as tmp:
                code, result, req = self.cli(self.audio_args(tmp, "run", "transcribe") + ["--wallet-limit", "1"],
                                             [self.wallet_limits(), rows, answer], ("NEURALDEEP_API_KEY", ["wallet-secret"]), "whisper")
                self.assertEqual((code, result["status"]), (1, "reconciliation_required"))
                self.assertIn("transcription", result["errors"][0].lower())
                self.assertFalse(Path(tmp, "result.json").exists())

    def test_the_wallet_pays_for_a_speechcore_upload_and_polls_the_job(self):
        rows = {"prices": [{"model": "speechcore", "billing": "minute", "rub_per_min": 1.0, "premium": False}]}
        transcript = json.dumps({"duration": 3.0, "segments": [{"start": 0.0, "end": 3.0, "text": "Hi"}]}).encode()
        with tempfile.TemporaryDirectory() as tmp:
            code, result, req = self.cli(self.audio_args(tmp, "run", "upload") + ["--wallet-limit", "3"],
                                         [self.wallet_limits(), rows, {"task_id": "job-fixture"}, {"status": "completed"},
                                          (transcript, "application/json")],
                                         ("NEURALDEEP_SPEECHCORE_API_KEY", ["wallet-secret"]), "speechcore")
            self.assertEqual(Path(tmp, "result.json").read_bytes(), transcript)
        self.assertEqual((code, result["status"], result["provider_job_id"]), (0, "completed", "job-fixture"))
        self.assertEqual([c.args[:2] for c in req.call_args_list][2:], [
            ("POST", client.SPEECH + "/upload"), ("GET", client.SPEECH + "/transcriptions/job-fixture/status"),
            ("GET", client.SPEECH + "/transcriptions/job-fixture")])
        self.assertEqual(result["quota_observed"]["estimate_rub"], 3.0)

    def test_subscription_keys_still_cannot_upload_to_speechcore_or_transcribe(self):
        for service, op, price in (("whisper", "transcribe", "gigaam-v3"), ("speechcore", "upload", "speechcore")):
            prices = {"prices": [{"model": price, "premium": False}]}
            with self.subTest(service=service), tempfile.TemporaryDirectory() as tmp:
                code, result, req = self.cli(self.audio_args(tmp, "run", op), [limits(), prices],
                                             ("NEURALDEEP_API_KEY", ["subscription-secret"]), service)
                self.assertEqual((code, result["status"]), (1, "blocked"))
                self.assertIn("remaining-quota endpoint unverified", result["errors"][0])
                self.assertNotIn("POST", [c.args[0] for c in req.call_args_list])

    def test_minute_billed_services_need_minutes(self):
        rows = {"prices": [{"model": "speechcore", "billing": "minute", "rub_per_min": 1.0, "premium": False}]}
        code, result, _ = self.cli(["check"], [self.wallet_limits(), rows], ("NEURALDEEP_API_KEY", ["wallet-secret"]), "speechcore")
        self.assertEqual((code, result["status"]), (1, "blocked"))
        self.assertIn("Positive integer", result["errors"][0])

    def test_wallet_estimates_follow_the_quota_units_and_price_kind(self):
        ocr = {"prices": [{"model": "ocr:extract", "billing": "unit", "unit_rub": 1.0, "premium": False}]}
        quote = client.wallet_quote("ocr", "extract", self.wallet_limits(), ocr, 6)  # 3 pages, pro
        self.assertEqual((quote["units"], quote["estimate_rub"]), (6, 6.0))
        minute = {"prices": [{"model": "audio:diarize", "billing": "minute", "rub_per_min": 0.333, "premium": False}]}
        self.assertEqual(client.wallet_quote("diarize", "diarize", self.wallet_limits(), minute, 3)["estimate_rub"], 1.0)
        with self.assertRaises(client.Blocked):
            client.wallet_quote("diarize", "diarize", self.wallet_limits(),
                                {"prices": [{"model": "audio:diarize", "billing": "token", "premium": False}]}, 3)

    def test_wallet_limit_must_be_a_positive_number(self):
        import io
        import sys
        for value in ("0", "-1", "nan", "inf", "x"):
            with self.subTest(value=value), patch.object(client, "SERVICE", "diarize"), patch.object(client, "request") as req, \
                    patch.object(sys, "argv", ["client.py", "check", "--wallet-limit", value]), \
                    patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as raised:
                    client.main()
            self.assertEqual(raised.exception.code, 2)
            req.assert_not_called()

    def test_the_subscription_guard_names_the_failing_condition(self):
        price = {"prices": [{"model": "audio:diarize", "premium": False}]}
        for changes, reason in ((("key", "billing_mode", "trial"), "billing mode"), (("key", "status", "disabled"), "ok key"),
                                ((None, "tier", "pro"), "Starter tier")):
            lim = limits()
            (lim[changes[0]] if changes[0] else lim)[changes[1]] = changes[2]
            with self.subTest(reason=reason), self.assertRaises(client.Blocked) as raised:
                client.guard("diarize", "diarize", lim, price, self.diarize_quota(), 1)
            self.assertIn(reason, str(raised.exception))

    def test_docs_explain_paying_from_the_wallet(self):
        import re
        root = Path(__file__).parents[1]
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        match = re.search(r"^## Paying from the wallet\n(.*?)(?=^## )", skill, re.M | re.S)
        self.assertIsNotNone(match, "SKILL.md has no Paying from the wallet section")
        for needle in ("wallet_approval_required", "--wallet-limit", "explicit yes", "Never pass `--wallet-limit` on your own",
                       "prefers a subscription key", "spending cap"):
            self.assertIn(needle, match.group(1))
        self.assertIn("wallet_approval_required", (root / "STARTER_RELAY.md").read_text(encoding="utf-8"))

    def test_merge_cli_is_offline_and_writes_a_hashed_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, result, output = self.merge_cli(tmp, self.diarization(), self.transcript_json())
            data = output.read_bytes()
            merged = json.loads(data.decode("utf-8"))
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["service"], "diarize")
            self.assertIsNone(result["provider_job_id"])
            self.assertEqual(result["artifacts"][0]["sha256"], client.hashlib.sha256(data).hexdigest())
            self.assertEqual(result["artifacts"][0]["provenance"]["trust"], "untrusted_data")
            self.assertEqual(merged["turns"], client.merge_transcript(self.diarization(), self.transcript_json())["turns"])
            sources = merged["sources"]
            for name in ("diarization", "transcript"):
                self.assertEqual(sources[name], client.hashlib.sha256(Path(tmp, name + ".json").read_bytes()).hexdigest())
            self.assertNotIn("Hello", json.dumps(result))

    def test_merge_cli_blocks_bad_input_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, result, output = self.merge_cli(tmp, self.diarization(), {"segments": "x"})
            self.assertEqual((code, result["status"]), (1, "blocked"))
            self.assertIn("transcript", result["errors"][0].lower())
            self.assertFalse(output.exists())
        with tempfile.TemporaryDirectory() as tmp:
            same = str(Path(tmp) / "transcript.json")
            code, result, _ = self.merge_cli(tmp, self.diarization(), self.transcript_json(), output=same)
            self.assertEqual((code, result["status"]), (1, "blocked"))
            self.assertEqual(json.loads(Path(same).read_text(encoding="utf-8")), self.transcript_json())
        for fmt, transcript in (("json", self.transcript_json()), ("rttm", None)):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as tmp:
                same = str(Path(tmp) / "diarization.json")
                code, result, _ = self.merge_cli(tmp, self.diarization(), transcript, output=same, extra=["--format", fmt])
                self.assertEqual((code, result["status"]), (1, "blocked"))
                self.assertEqual(json.loads(Path(same).read_text(encoding="utf-8")), self.diarization())

    # Third cross-review (2026-10-18).
    def test_merge_insists_on_utf8(self):
        cases = {"utf-16": json.dumps(self.transcript_json()).encode("utf-16"),
                 "utf-32": json.dumps(self.transcript_json()).encode("utf-32"),
                 "cesu-8 surrogate": b'{"segments": [{"start": 0, "end": 1, "text": "\xed\xa0\x80"}]}'}
        for case, data in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "transcript.json").write_bytes(data)
                code, result, _ = self.merge_cli(tmp, self.diarization(), None, extra=["--transcript", str(Path(tmp, "transcript.json"))])
                self.assertEqual((code, result["status"]), (1, "blocked"))
                self.assertEqual(result["errors"], ["Transcript file is not valid UTF-8 JSON"])

    def test_merge_local_failures_name_the_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "absent.json")
            code, result, _ = self.merge_cli(tmp, self.diarization(), None, extra=["--transcript", missing])
            self.assertEqual(result["errors"], ["Cannot read the transcript file"])
            code, result, _ = self.merge_cli(tmp, self.diarization(), self.transcript_json(), output=str(Path(tmp) / "no" / "out.json"))
            self.assertEqual(result["errors"], ["Cannot write the merge output"])
            lone = Path(tmp) / "lone.json"  # a valid JSON escape for an unpaired surrogate
            lone.write_text('{"segments": [{"start": 0.0, "end": 2.0, "text": "\\ud83d"}]}', encoding="utf-8")
            code, result, _ = self.merge_cli(tmp, self.diarization(), None, extra=["--transcript", str(lone)])
            self.assertEqual(result["errors"], ["Input text is not valid Unicode"])
            self.assertEqual(code, 1)

if __name__ == "__main__":
    unittest.main()
