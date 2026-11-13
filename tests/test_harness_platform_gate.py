"""The optional tool runtime must be exercised on both supported Linux architectures."""
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class HarnessPlatformGateTests(unittest.TestCase):
    def test_native_architectures_run_the_real_offline_contract(self):
        path = ROOT / ".github/workflows/harness-platforms.yml"
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertIn("workflow_call", data["on"])
        job = data["jobs"]["harness"]
        self.assertEqual(set(job["strategy"]["matrix"]["os"]),
                         {"ubuntu-latest", "ubuntu-24.04-arm"})
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("docker build", commands)
        self.assertIn("/opt/contract/tests/smoke.py", commands)
        for flag in ("--network none", "--read-only", "--cap-drop ALL", "--pids-limit"):
            self.assertIn(flag, commands)
        self.assertNotIn("--privileged", commands)
        self.assertNotIn("docker.sock", commands)
        self.assertNotIn("secrets.", commands)
        self.assertTrue(any(step.get("uses", "").startswith("actions/upload-artifact@")
                            for step in job["steps"]))
        self.assertFalse(any(step.get("continue-on-error") == "true" for step in job["steps"]))

    def test_release_cannot_skip_harness_architecture_gate(self):
        data = yaml.load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"),
                         Loader=yaml.BaseLoader)
        self.assertIn("harness-smoke", data["jobs"]["release"]["needs"])
        self.assertEqual(data["jobs"]["harness-smoke"]["uses"],
                         "./.github/workflows/harness-platforms.yml")
        self.assertNotIn("if", data["jobs"]["harness-smoke"])


if __name__ == "__main__":
    unittest.main()
