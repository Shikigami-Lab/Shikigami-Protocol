"""Release checks must run on the tagged revision and fail closed."""
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    # BaseLoader preserves GitHub's YAML `on` key instead of treating it as True.
    return yaml.load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"),
                     Loader=yaml.BaseLoader)


class ReleaseGateTests(unittest.TestCase):
    def test_ci_and_package_gate_use_python_312(self):
        for name, job in (("ci.yml", "test-and-lint"), ("source-package.yml", "source-package")):
            with self.subTest(workflow=name):
                steps = workflow(name)["jobs"][job]["steps"]
                versions = [step["with"]["python-version"] for step in steps
                            if "actions/setup-python@" in step.get("uses", "")]
                self.assertEqual(versions, ["3.12"])

    def test_ci_discovers_unit_tests_with_node_available(self):
        ci = workflow("ci.yml")
        self.assertIn("workflow_call", ci["on"])
        steps = ci["jobs"]["test-and-lint"]["steps"]
        tests = [i for i, step in enumerate(steps)
                 if "unittest discover" in step.get("run", "")]
        self.assertTrue(tests, "CI must actually execute the unit suite")
        self.assertTrue(any(step.get("uses", "").startswith("actions/setup-node@")
                            for step in steps[:tests[0]]))
        for i in tests:
            self.assertNotEqual(steps[i].get("continue-on-error"), "true")

    def test_release_requires_all_reusable_checks_to_succeed(self):
        jobs = workflow("release.yml")["jobs"]
        release = jobs["release"]
        needed = release.get("needs", [])
        if isinstance(needed, str):
            needed = [needed]
        callees = {jobs[job].get("uses") for job in needed}
        self.assertEqual(callees, {"./.github/workflows/ci.yml",
                                   "./.github/workflows/docker-test.yml",
                                   "./.github/workflows/harness-platforms.yml",
                                   "./.github/workflows/source-package.yml"})
        self.assertIn("harness-smoke", needed)
        for job in needed:
            self.assertNotEqual(jobs[job].get("continue-on-error"), "true")
            self.assertNotIn("if", jobs[job], "required gates must not be conditionally skipped")
        condition = release.get("if", "")
        self.assertIn("success()", condition)
        self.assertIn("refs/tags/v", condition)
        self.assertNotIn("always()", condition)

    def test_cd_runs_for_dev_pushes_tags_and_dispatch(self):
        events = workflow("release.yml")["on"]
        self.assertEqual(events["push"]["branches"], ["dev"])
        self.assertEqual(events["push"]["tags"], ["v*"])
        self.assertIn("workflow_dispatch", events)

    def test_dev_cannot_publish_a_release(self):
        jobs = workflow("release.yml")["jobs"]
        self.assertEqual(set(jobs), {"unit-tests", "docker-smoke", "harness-smoke", "source-package", "release"})
        release = jobs["release"]
        self.assertEqual(release["if"],
                         "${{ success() && startsWith(github.ref, 'refs/tags/v') }}")
        publishers = [step for step in release["steps"]
                      if "action-gh-release@" in step.get("uses", "")]
        self.assertEqual(len(publishers), 1)
        self.assertEqual(publishers[0]["with"]["draft"], "true")

    def test_tag_draft_attaches_exact_linux_package_from_this_run(self):
        steps = workflow("release.yml")["jobs"]["release"]["steps"]
        download = next(step for step in steps
                        if step.get("uses", "").startswith("actions/download-artifact@"))
        self.assertEqual(download["with"]["name"], "source-package-Linux-X64-${{ github.sha }}")
        self.assertNotIn("run-id", download["with"])
        self.assertNotIn("github-token", download["with"])
        self.assertNotIn("repository", download["with"])
        self.assertNotIn("pattern", download["with"])
        release = next(step for step in steps if "action-gh-release@" in step.get("uses", ""))
        self.assertLess(steps.index(download), steps.index(release))
        self.assertEqual(set(release["with"]["files"].splitlines()),
                         {"tested-source/source-package/source.zip", "tested-source/source-package/manifest.json"})
        self.assertEqual(download["with"]["path"], "tested-source")
        self.assertEqual(release["with"]["fail_on_unmatched_files"], "true")
        self.assertFalse(any("source_package.py" in step.get("run", "") for step in steps))

    def test_package_gate_is_reusable_with_windows_linux_and_both_mac_architectures(self):
        package = workflow("source-package.yml")
        self.assertIn("workflow_call", package["on"])
        self.assertIn("workflow_dispatch", package["on"])
        self.assertEqual(package["permissions"], {"contents": "read"})
        job = package["jobs"]["source-package"]
        self.assertEqual(job["strategy"]["matrix"]["os"],
                         ["ubuntu-latest", "windows-latest", "macos-latest", "macos-15-intel"])
        self.assertEqual(job["runs-on"], "${{ matrix.os }}")
        self.assertEqual(job["strategy"]["fail-fast"], "false")
        steps = job["steps"]
        checkout = next(step for step in steps if "actions/checkout@" in step.get("uses", ""))
        self.assertEqual(checkout["with"]["fetch-depth"], "0")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        setup = next(step for step in steps if "actions/setup-python@" in step.get("uses", ""))
        self.assertEqual(setup["with"]["python-version"], "3.12")
        runner = next(step for step in steps if "run_package_acceptance.py" in step.get("run", ""))
        self.assertLess(steps.index(checkout), steps.index(setup))
        self.assertLess(steps.index(setup), steps.index(runner))
        self.assertIn("python .github/scripts/run_package_acceptance.py", runner["run"])
        self.assertIn('runner.os', runner["run"])
        self.assertIn('xvfb-run -a', runner["run"])
        node = next(step for step in steps if "actions/setup-node@" in step.get("uses", ""))
        self.assertEqual(node["with"]["node-version"], "22")
        self.assertLess(steps.index(node), steps.index(runner))
        self.assertNotIn("--no-sandbox", str(steps))

    def test_package_artifact_has_sha_identity_and_preserves_failure_diagnostics(self):
        job = workflow("source-package.yml")["jobs"]["source-package"]
        uploads = [step for step in job["steps"] if "actions/upload-artifact@" in step.get("uses", "")]
        self.assertEqual(len(uploads), 1)
        upload = uploads[0]
        self.assertEqual(upload["if"], "${{ always() }}")
        self.assertEqual(upload["with"]["name"], "source-package-${{ runner.os }}-${{ runner.arch }}-${{ github.sha }}")
        self.assertEqual(set(upload["with"]["path"].splitlines()), {
            "${{ runner.temp }}/source-package/source.zip",
            "${{ runner.temp }}/source-package/manifest.json",
            "${{ runner.temp }}/source-package-acceptance/acceptance.json",
            "${{ runner.temp }}/source-package-acceptance/**/*.log",
            "${{ runner.temp }}/source-package-acceptance/desktop/*.json",
            "${{ runner.temp }}/source-package-acceptance/desktop/*.png",
        })
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertNotIn("overwrite", upload["with"])

    def test_package_and_release_gates_do_not_ignore_failure_or_inherit_secrets(self):
        for name in ("source-package.yml", "release.yml"):
            data = workflow(name)
            for job in data["jobs"].values():
                self.assertNotEqual(job.get("continue-on-error"), "true")
                self.assertNotIn("secrets", job)
                for step in job.get("steps", []):
                    self.assertNotEqual(step.get("continue-on-error"), "true")
                    if "run" in step:
                        self.assertNotIn("||", step["run"])
            if name == "source-package.yml":
                self.assertNotIn("secrets.", str(data))

    def test_docker_check_uses_strict_probe_without_ignored_failure(self):
        docker = workflow("docker-test.yml")
        self.assertIn("workflow_call", docker["on"])
        steps = docker["jobs"]["docker-test"]["steps"]
        probes = [s for s in steps if "docker_smoke.py" in s.get("run", "")]
        self.assertEqual(len(probes), 1)
        self.assertNotIn("||", probes[0]["run"])
        self.assertNotEqual(probes[0].get("continue-on-error"), "true")
        paths = docker["on"]["push"].get("paths", [])
        self.assertIn(".github/workflows/docker-test.yml", paths)
        self.assertIn(".github/scripts/**", paths)


if __name__ == "__main__":
    unittest.main()
