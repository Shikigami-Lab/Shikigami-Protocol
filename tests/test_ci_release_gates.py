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

    def test_release_requires_both_reusable_checks_to_succeed(self):
        jobs = workflow("release.yml")["jobs"]
        release = jobs["release"]
        needed = release.get("needs", [])
        if isinstance(needed, str):
            needed = [needed]
        callees = {jobs[job].get("uses") for job in needed}
        self.assertEqual(callees, {"./.github/workflows/ci.yml",
                                   "./.github/workflows/docker-test.yml"})
        for job in needed:
            self.assertNotEqual(jobs[job].get("continue-on-error"), "true")
        condition = release.get("if", "")
        self.assertIn("success()", condition)
        self.assertIn("refs/tags/v", condition)
        self.assertNotIn("always()", condition)

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
