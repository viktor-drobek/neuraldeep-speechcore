import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

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
                p.write_text(json.dumps({"api_key": value}))
                self.assertEqual(client.resolve_key(), "env-fixture")
            p.write_text('{"api_key": "file-fixture"}')
            self.assertEqual(client.resolve_key(), "file-fixture")
            p.write_text('{}')
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

    def test_poll_failure_and_deadline_never_fetch(self):
        for status in ("failed", "cancelled", "unknown"):
            get = Mock(return_value={"status": status})
            with self.assertRaises(client.JobError):
                client.poll(get, "/status", "/result", "completed", timeout=10)
            self.assertEqual(get.call_count, 1)
        get = Mock()
        with self.assertRaises(client.PollTimeout):
            client.poll(get, "/status", "/result", "completed", timeout=0)
        get.assert_not_called()

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
        for error in (HTTPError("https://example.com", 503, "secret-fixture", {}, None), URLError("secret-fixture")):
            opener = Mock()
            opener.open.side_effect = error
            with self.assertRaises(client.RequestError) as raised:
                client.request("POST", client.HUB + "/search/web", "secret-fixture", b"{}", opener=opener)
            self.assertNotIn("secret-fixture", str(raised.exception))
            self.assertEqual(opener.open.call_count, 1)

    def test_durable_state_and_duplicate_prevention(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            state = client.new_state(path, "task-fixture", "images")
            state["provider_job_id"] = "provider-fixture"
            client.save_state(path, state)
            self.assertEqual(json.loads(path.read_text())["provider_job_id"], "provider-fixture")
            with self.assertRaises(FileExistsError):
                client.new_state(path, "task-fixture", "images")

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
                    self.assertEqual(json.loads(Path(args.state).read_text())["status"], "reconciliation_required")
                    return {"task_uid": "job-fixture"}
                self.assertEqual(json.loads(Path(args.state).read_text())["provider_job_id"], "job-fixture")
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

    def test_artifact_has_hash_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = client.save_artifact(Path(tmp) / "result.bin", b"test", "/result")
            self.assertEqual(artifact["sha256"], "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08")
            self.assertEqual(artifact["provenance"]["endpoint"], "/result")
            self.assertEqual(artifact["provenance"]["trust"], "untrusted_data")


if __name__ == "__main__":
    unittest.main()
