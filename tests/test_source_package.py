"""Build and unpack release sources from disposable Git repositories."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from zipfile import ZipFile, ZipInfo
import stat

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("source_package", ROOT / ".github/scripts/source_package.py")


def load_builder():
    module = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(module)
    return module


class SourcePackageTests(unittest.TestCase):
    def test_archive_rejects_traversal_links_and_case_collisions(self):
        builder = load_builder()
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name, entries in (
            ("traversal", [("../outside.txt", "bad")]),
            ("backslash", [("folder\\outside.txt", "bad")]),
            ("collision", [("same.txt", "one"), ("SAME.txt", "two")]),
        ):
            archive = root / (name + ".zip")
            with ZipFile(archive, "w") as out:
                for path, text in entries:
                    info = ZipInfo(path)
                    info.filename = path  # Preserve the literal on Windows for the malformed fixture.
                    out.writestr(info, text)
            with self.subTest(case=name), self.assertRaises(ValueError):
                builder.extract_checked(archive, root / name)
        archive = root / "link.zip"
        with ZipFile(archive, "w") as out:
            info = ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            out.writestr(info, "../outside")
        with self.assertRaises(ValueError):
            builder.extract_checked(archive, root / "link")

    def test_package_contains_only_committed_files_and_a_matching_manifest(self):
        builder = load_builder()
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = root / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        for relative in builder.REQUIRED:
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps({"version": "1.2.3"}) if relative == "package.json" else "# fixture\n"
            if relative == "scripts/leak_check.py":
                text = "print('synthetic checker passed')\n"
            path.write_text(text, encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Fixture"], check=True)
        (source / "local-secret.txt").write_text("private fixture", encoding="utf-8")
        output = root / "package"
        result = builder.build_package(source, output)
        self.assertEqual(result["version"], "1.2.3")
        with ZipFile(output / "source.zip") as archive:
            self.assertNotIn("local-secret.txt", archive.namelist())
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["commit"], result["commit"])
        self.assertIn("server.py", manifest["files"])
        self.assertEqual((output / "extracted/package.json").read_text(encoding="utf-8"), '{"version": "1.2.3"}')
        with self.assertRaises(ValueError):
            builder.build_package(source, source / "nested-output")
