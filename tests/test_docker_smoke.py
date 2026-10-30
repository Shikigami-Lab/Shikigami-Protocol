"""Offline contract tests for the disposable Docker readiness check."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
import urllib.error


SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/docker_smoke.py"


class SmokeTestCase(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "Docker smoke helper has not been implemented")
        spec = importlib.util.spec_from_file_location("docker_smoke", SCRIPT)
        self.smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.smoke)


class DockerSmokeTests(SmokeTestCase):
    def setUp(self):
        super().setUp()
        self.now = 0.0
        self.calls = []
        self.output = io.StringIO()
        self.responses = {}
        self.argv = ["--image", "smoke-fixture:test", "--timeout", "3",
                     "--interval", "1", "--expected-version", "1.2.3"]
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(self.output))
        stack.enter_context(contextlib.redirect_stderr(self.output))
        stack.enter_context(patch.object(self.smoke.time, "monotonic", side_effect=lambda: self.now))
        stack.enter_context(patch.object(self.smoke.time, "sleep", side_effect=self.sleep))
        self.docker_run = stack.enter_context(patch.object(self.smoke.subprocess, "run", side_effect=self.docker))

    def sleep(self, seconds):
        self.assertGreater(seconds, 0)
        self.now += seconds

    def docker(self, args, **kwargs):
        self.calls.append((args, kwargs, self.now))
        self.assertEqual(args[0], "docker")
        self.assertFalse(kwargs.get("shell", False))
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], 10)
        if args[1] not in ("logs", "rm"):
            self.assertLessEqual(kwargs["timeout"], 3 - self.now)
        response = self.responses.get(args[1])
        if callable(response):
            return response(args, kwargs)
        if isinstance(response, BaseException):
            raise response
        if response is not None:
            return subprocess.CompletedProcess(args, *response)
        return subprocess.CompletedProcess(args, 0, "true\n" if args[1] == "inspect" else "fixture\n", "")

    def commands(self, verb):
        return [args for args, _, _ in self.calls if args[1] == verb]

    def assert_cleaned(self):
        runs = self.commands("run")
        name = runs[0][runs[0].index("--name") + 1]
        self.assertEqual(self.commands("rm"), [["docker", "rm", "--force", name]])
        for command in self.commands("inspect") + self.commands("logs"):
            self.assertEqual(command[-1], name)
        return name

    def test_success_checks_running_before_and_after_probe_and_cleans(self):
        self.assertEqual(self.smoke.main(self.argv), 0)
        self.assertEqual([args[1] for args, _, _ in self.calls],
                         ["run", "inspect", "exec", "inspect", "rm"])
        name = self.assert_cleaned()
        command = self.commands("exec")[0]
        self.assertEqual(command[2:5], [name, "python", "-c"])
        self.assertEqual(command[5], self.smoke.PROBE)
        self.assertIn("1.2.3", command[6:])

    def test_container_is_isolated_without_ports_mounts_or_environment(self):
        self.assertEqual(self.smoke.main(self.argv), 0)
        run = self.commands("run")[0]
        self.assertIn("--detach", run)
        self.assertIn("--pull=never", run)
        self.assertEqual(run[run.index("--network") + 1], "none")
        for resource in ("--cpus", "--memory", "--pids-limit"):
            self.assertIn(resource, run)
        forbidden = {"-p", "-P", "--publish", "--publish-all", "-v", "--volume",
                     "--mount", "--volumes-from", "-e", "--env", "--env-file", "--privileged"}
        self.assertFalse(forbidden.intersection(arg.split("=", 1)[0] for arg in run))
        self.assertEqual(run[-1], "smoke-fixture:test")

    def test_container_names_are_unique_across_invocations(self):
        self.assertEqual(self.smoke.main(self.argv), 0)
        self.assertEqual(self.smoke.main(self.argv), 0)
        names = [args[args.index("--name") + 1] for args in self.commands("run")]
        self.assertNotEqual(*names)

    def test_expected_version_defaults_to_repository_package(self):
        self.assertEqual(self.smoke.main(self.argv[:-2]), 0)
        version = json.loads((SCRIPT.parents[2] / "package.json").read_text(encoding="utf-8"))["version"]
        self.assertIn(version, self.commands("exec")[0][6:])

    def test_failed_http_retries_until_deadline_then_logs_and_cleans(self):
        self.responses["exec"] = (1, "", "/version: HTTP Error 503")
        self.assertNotEqual(self.smoke.main(self.argv), 0)
        self.assertEqual(self.now, 3)
        self.assertEqual(len(self.commands("exec")), 3)
        self.assertIn("503", self.output.getvalue())
        self.assertIn("deadline", self.output.getvalue().lower())
        self.assertEqual(len(self.commands("logs")), 1)
        self.assert_cleaned()

    def test_recovery_before_deadline_can_succeed(self):
        def recovering(args, kwargs):
            return subprocess.CompletedProcess(args, int(self.now < 1), "", "warming up")
        self.responses["exec"] = recovering
        self.assertEqual(self.smoke.main(self.argv), 0)
        self.assertEqual(self.now, 1)
        self.assert_cleaned()

    def test_early_exit_does_not_attempt_http(self):
        self.responses["inspect"] = (0, "false\n", "")
        self.assertNotEqual(self.smoke.main(self.argv), 0)
        self.assertFalse(self.commands("exec"))
        self.assertIn("running", self.output.getvalue().lower())
        self.assert_cleaned()

    def test_exit_after_successful_http_is_failure(self):
        def inspect(args, kwargs):
            running = len(self.commands("inspect")) == 1
            return subprocess.CompletedProcess(args, 0, str(running).lower(), "")
        self.responses["inspect"] = inspect
        self.assertNotEqual(self.smoke.main(self.argv), 0)
        self.assert_cleaned()

    def test_start_failure_is_not_swallowed_and_cleanup_is_attempted(self):
        self.responses["run"] = (125, "", "fixture image unavailable")
        self.assertNotEqual(self.smoke.main(self.argv), 0)
        self.assertFalse(self.commands("inspect"))
        self.assertIn("fixture image unavailable", self.output.getvalue())
        self.assert_cleaned()

    def test_inspect_failure_is_fatal(self):
        self.responses["inspect"] = (1, "", "inspect denied")
        self.assertNotEqual(self.smoke.main(self.argv), 0)
        self.assertIn("inspect denied", self.output.getvalue())
        self.assert_cleaned()

    def test_subprocess_exceptions_fail_and_still_cleanup(self):
        for verb in ("run", "inspect", "exec"):
            for error in (OSError("fixture command error"),
                          subprocess.TimeoutExpired(["docker", verb], 1)):
                with self.subTest(verb=verb, error=type(error).__name__):
                    self.calls.clear()
                    self.responses = {verb: error}
                    self.assertNotEqual(self.smoke.main(self.argv), 0)
                    self.assert_cleaned()

    def test_success_after_deadline_is_rejected(self):
        def slow(args, kwargs):
            self.now = 3
            return subprocess.CompletedProcess(args, 0, "", "")
        self.responses["exec"] = slow
        self.assertNotEqual(self.smoke.main(self.argv), 0)
        self.assertIn("deadline", self.output.getvalue().lower())
        self.assert_cleaned()

    def test_cleanup_failure_prevents_false_green_and_prints_logs(self):
        for response in ((1, "", "fixture cleanup denied"), OSError("cleanup unavailable"),
                         subprocess.TimeoutExpired(["docker", "rm"], 1)):
            with self.subTest(response=response):
                self.calls.clear()
                self.responses = {"rm": response}
                self.assertNotEqual(self.smoke.main(self.argv), 0)
                self.assertIn("cleanup", self.output.getvalue().lower())
                self.assertEqual(len(self.commands("logs")), 1)
                self.assert_cleaned()

    def test_log_failure_does_not_prevent_cleanup(self):
        for response in ((1, "", "logs unavailable"), OSError("logs unavailable"),
                         subprocess.TimeoutExpired(["docker", "logs"], 1)):
            with self.subTest(response=response):
                self.calls.clear()
                self.responses = {"inspect": (0, "false", ""), "logs": response}
                self.assertNotEqual(self.smoke.main(self.argv), 0)
                self.assert_cleaned()

    def test_failure_logs_are_bounded(self):
        self.responses = {"exec": (1, "", "HTTP 503"),
                          "logs": (0, "x" * 100000, "y" * 100000)}
        self.assertNotEqual(self.smoke.main(self.argv), 0)
        command = self.commands("logs")[0]
        self.assertIn("--tail", command)
        self.assertLessEqual(int(command[command.index("--tail") + 1]), 100)
        self.assertLess(len(self.output.getvalue()), 10000)
        self.assert_cleaned()

    def test_invalid_arguments_do_not_touch_docker(self):
        cases = [[], ["--image", ""], ["--image", "--privileged"],
                 ["--image", "bad image"], ["--image", "fixture", "--expected-version", " "]]
        for option in ("--timeout", "--interval"):
            for value in ("0", "-1", "nan", "inf", "-inf", "bad"):
                cases.append(["--image", "fixture", option + "=" + value])
        for args in cases:
            with self.subTest(args=args):
                self.assertNotEqual(self.smoke.main(args), 0)
        self.docker_run.assert_not_called()

    def test_invalid_package_version_does_not_touch_docker(self):
        for document in ('{"version": null}', '{}', '[]', '{', '{"version": ""}'):
            with self.subTest(document=document), patch.object(self.smoke.Path, "read_text", return_value=document):
                self.assertNotEqual(self.smoke.main(self.argv[:-2]), 0)
        self.docker_run.assert_not_called()


class ProbeTests(SmokeTestCase):
    """Execute the actual container-side program with a simulated urllib transport."""

    def probe(self, version=b'{"version": "1.2.3"}', root_status=200, error_path=None):
        opened = []
        output = io.StringIO()

        def open_url(url, timeout):
            opened.append((url, timeout))
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 2)
            if url.endswith(error_path or "never"):
                raise urllib.error.HTTPError(url, 503, "Unavailable", {}, None)
            response = Mock()
            response.status = 200 if url.endswith("/version") else root_status
            response.read.side_effect = lambda size=-1: version[:size] if size >= 0 else version
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            return response

        opener = Mock()
        opener.open.side_effect = open_url
        with patch("urllib.request.build_opener", return_value=opener), \
                patch.object(sys, "argv", ["-c", "1.2.3", "2"]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                exec(compile(self.smoke.PROBE, "<container-probe>", "exec"), {"__name__": "__main__"})
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, opened, output.getvalue()

    def test_probe_checks_both_endpoints(self):
        code, opened, _ = self.probe()
        self.assertEqual(code, 0)
        self.assertEqual([url for url, _ in opened],
                         ["http://127.0.0.1:7788/version", "http://127.0.0.1:7788/"])

    def test_probe_rejects_http_errors_on_either_endpoint(self):
        for path in ("/version", "/"):
            with self.subTest(path=path):
                code, _, output = self.probe(error_path=path)
                self.assertNotEqual(code, 0)
                self.assertIn("503", output)

    def test_probe_rejects_non_success_status(self):
        for status in (301, 404, 503):
            with self.subTest(status=status):
                self.assertNotEqual(self.probe(root_status=status)[0], 0)

    def test_probe_rejects_invalid_json_and_incorrect_version(self):
        for body in (b'not-json', b'[]', b'null', b'{}', b'{"version": "9.9.9"}',
                     b'{"version": 123}', b'{"version": null}', b'{"version": "v1.2.3"}',
                     b'{"version": "1.2.3", "bad": NaN}', b'\xff'):
            with self.subTest(body=body):
                code, _, output = self.probe(version=body)
                self.assertNotEqual(code, 0)
                self.assertTrue(output.strip())

    def test_probe_rejects_oversized_json(self):
        body = json.dumps({"version": "1.2.3", "padding": "x" * 100000}).encode()
        self.assertNotEqual(self.probe(version=body)[0], 0)


if __name__ == "__main__":
    unittest.main()
