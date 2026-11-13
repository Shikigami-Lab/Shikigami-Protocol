"""Build the committed source ZIP and validate the exact extracted package."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import zipfile

REQUIRED = (
    "package.json", "server.py", "requirements.txt", "init.bat", "init.sh",
    "launch.bat", "launch.sh", "README.md", "README_zh.md", "LICENSE",
    "static/index.html", "scripts/leak_check.py",
)
MAX_UNPACKED = 256 * 1024 * 1024


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else _hash_stream(stream)


def _hash_stream(stream):
    value = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        value.update(block)
    return value.hexdigest()


def extract_checked(archive, target):
    """Reject unsafe paths before writing any member from a bounded archive."""
    archive, target = Path(archive), Path(target).resolve()
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if len(members) > 20000 or sum(member.file_size for member in members) > MAX_UNPACKED:
            raise ValueError("Source archive exceeds extraction limits")
        seen = set()
        for member in members:
            name = member.orig_filename
            path = PurePosixPath(name)
            parts = name.rstrip("/").split("/")
            key = name.rstrip("/").casefold()
            if (not parts or any(part in ("", ".", "..") for part in parts)
                    or path.is_absolute() or "\\" in name or ":" in name
                    or any(ord(char) < 32 for char in name)
                    or key in seen or stat.S_ISLNK(member.external_attr >> 16)
                    or not (target / path).resolve().is_relative_to(target)):
                raise ValueError("Unsafe source archive member")
            seen.add(key)
        target.mkdir(parents=True, exist_ok=False)
        for member in members:
            dest = target.joinpath(*PurePosixPath(member.filename).parts)
            if member.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as stream, dest.open("xb") as output:
                shutil.copyfileobj(stream, output)
    return target


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if result.returncode:
        raise ValueError("Git source packaging failed during " + args[0])
    return result.stdout


def build_package(source, output, ref="HEAD"):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Package output must be separate from the source checkout")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Package output must be empty")
    commit = git(source, "rev-parse", "--verify", ref + "^{commit}").decode().strip()
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Could not resolve a source commit")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "source.zip"
    git(source, "archive", "--format=zip", "--output=" + str(archive), commit)
    extracted = extract_checked(archive, output / "extracted")
    for relative in REQUIRED:
        if not (extracted / relative).is_file():
            raise ValueError("Source package is missing " + relative)
    package = json.loads((extracted / "package.json").read_text(encoding="utf-8"))
    version = package.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise ValueError("Source package has no valid version")
    check = subprocess.run([sys.executable, str(extracted / "scripts/leak_check.py"),
                            "--root", str(extracted)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    (output / "leak-check.log").write_text(check.stdout + check.stderr, encoding="utf-8")
    if check.returncode:
        raise ValueError("Source package failed its privacy audit")
    files = {path.relative_to(extracted).as_posix(): {"bytes": path.stat().st_size, "sha256": digest(path)}
             for path in sorted(extracted.rglob("*")) if path.is_file()}
    manifest = {"version": version, "commit": commit, "zip_sha256": digest(archive), "files": files}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Source package verified: {version}, commit {commit}, {len(files)} files")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ref", default="HEAD")
    args = parser.parse_args()
    try:
        build_package(args.source, args.output, args.ref)
    except (OSError, ValueError, subprocess.TimeoutExpired, zipfile.BadZipFile):
        print("Source packaging failed. Inspect the package checks and output logs.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
