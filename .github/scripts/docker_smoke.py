"""Check an isolated image; readiness timeout excludes bounded logs/cleanup.

No host ports, volumes, credentials or application environment are forwarded.
Failure diagnostics and cleanup can add at most five seconds each.
"""

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import uuid


PROBE = r'''
import json
import sys
import urllib.request


def invalid_constant(value):
    raise ValueError("invalid JSON constant: " + value)


try:
    expected, timeout = sys.argv[1], float(sys.argv[2])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for path in ("/version", "/"):
        with opener.open("http://127.0.0.1:7788" + path, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise ValueError(path + ": HTTP " + str(response.status))
            if path == "/version":
                body = response.read(65537)
                if len(body) > 65536:
                    raise ValueError("/version: JSON exceeds 64 KiB")
                data = json.loads(body, parse_constant=invalid_constant)
                if not isinstance(data, dict) or data.get("version") != expected:
                    raise ValueError("/version: expected version " + repr(expected)
                                     + ", received " + repr(data)[:200])
    print("HTTP readiness passed")
except Exception as exc:
    print("HTTP probe failed: " + str(exc)[:1000], file=sys.stderr)
    sys.exit(1)
'''


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def docker(args, timeout):
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("docker " + args[0] + " timed out") from exc
    except OSError as exc:
        raise RuntimeError("docker " + args[0] + ": " + str(exc)) from exc


def checked(result, operation):
    if result.returncode:
        detail = (result.stderr or result.stdout or "no output")[-1500:]
        raise RuntimeError(f"{operation} failed ({result.returncode}): {detail}")
    return result


def show_logs(name):
    try:
        result = checked(docker(["logs", "--tail", "50", name], 5), "docker logs")
        print("Container logs (last 50 lines, at most 4000 characters):", file=sys.stderr)
        print((result.stdout + result.stderr)[-4000:], file=sys.stderr)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--timeout", type=positive_seconds, default=90)
    parser.add_argument("--interval", type=positive_seconds, default=1)
    parser.add_argument("--expected-version")
    try:
        args = parser.parse_args(argv)
        if not args.image or args.image.startswith("-") or any(c.isspace() for c in args.image):
            parser.error("--image must be a nonempty image reference without whitespace")
        expected = args.expected_version
        if expected is None:
            package = Path(__file__).resolve().parents[2] / "package.json"
            expected = json.loads(package.read_text(encoding="utf-8"))["version"]
        if not isinstance(expected, str) or not expected.strip() or expected != expected.strip():
            parser.error("expected version must be a nonempty string without surrounding whitespace")
    except SystemExit as exc:
        return exc.code
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("Cannot read expected version: " + str(exc), file=sys.stderr)
        return 2

    name = "shikigami-smoke-" + uuid.uuid4().hex
    deadline = time.monotonic() + args.timeout
    last_error = "no successful HTTP probe"

    def budget():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Readiness deadline exceeded: " + last_error)
        return min(10, remaining)

    def running():
        state = checked(docker(["inspect", "--format", "{{.State.Running}}", name], budget()),
                        "docker inspect")
        if state.stdout.strip() != "true":
            raise RuntimeError("Container is not running (exited before readiness completed)")

    success = False
    try:
        checked(docker(["run", "--detach", "--pull=never", "--name", name,
                        "--network", "none", "--cpus", "2", "--memory", "2g",
                        "--pids-limit", "256", args.image], budget()), "docker run")
        while True:
            running()
            remaining = budget()
            result = docker(["exec", name, "python", "-c", PROBE,
                             expected, str(min(2, remaining / 3))], remaining)
            if result.returncode == 0:
                running()
                budget()  # Even a successful command cannot exceed the overall deadline.
                success = True
                break
            last_error = (result.stderr or result.stdout or
                          f"docker exec returned {result.returncode}")[-1500:]
            time.sleep(min(args.interval, budget()))
    except RuntimeError as exc:
        print("Docker smoke failed: " + str(exc), file=sys.stderr)
    finally:
        if not success:
            show_logs(name)
        try:
            checked(docker(["rm", "--force", name], 5), "cleanup")
        except RuntimeError as exc:
            print("Container cleanup failed: " + str(exc), file=sys.stderr)
            if success:
                show_logs(name)
            success = False

    if success:
        print("Docker smoke passed: HTTP endpoints, version " + expected + ", and cleanup")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
