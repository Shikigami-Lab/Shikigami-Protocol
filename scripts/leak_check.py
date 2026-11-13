"""Offline, bounded release audit; not a guarantee that a tree contains no secrets.

Check tracked working-tree files and commit messages not reachable from local
remote-tracking refs (never fetch). Without .git, audit source files and explicitly
skip commits. Recognize common credential formats, literal secret assignments,
private keys and known local/runtime artifacts. Synthetic placeholder prefixes
are accepted; test directories receive the same checks as application source.
Diagnostics contain only path/commit, line and kind, never matched source text.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess


EXAMPLES = {"example_luna.json", "example_luna_en.json", "example_mochi.json", "example_mochi_en.json"}
SOURCE_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
TOKENS = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{35}|"
    r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[A-Z0-9]{16})\b"
)
ASSIGNMENT = re.compile(
    r'''(?ix)["']?\b(?:[a-z0-9]+_)*(?:api_?key|access_token|auth_token|token|password|secret)\b["']?'''
    r'''\s*[:=]\s*(?:["']([^"'\r\n]+)["']|([A-Za-z0-9_/+\-=]{20,})(?=\s*(?:[#;].*)?$))'''
)
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")


def placeholder(value):
    value = value.lower()
    return (value.startswith(("test-", "test_", "fixture-", "fixture_", "synthetic-", "dummy-",
                              "example-", "example_", "fake-", "fake_", "fake\\", "your-", "your_", "sk-test-", "sk-dummy-",
                              "sk-example-", "${", "<"))
            or value in {"changeme", "placeholder", "ollama"})


def content_findings(text):
    for number, line in enumerate(text.splitlines(), 1):
        if PRIVATE_KEY.search(line):
            yield number, "private-key"
        credential = any(not placeholder(match.group()) for match in TOKENS.finditer(line))
        for match in ASSIGNMENT.finditer(line):
            value = match.group(1) or match.group(2)
            # Bare identifiers, method calls and short natural-language markers
            # are common in source/tests. Only consider long mixed literals.
            if (len(value) >= 20 and not placeholder(value)
                    and re.search(r"[A-Za-z]", value) and re.search(r"\d", value)
                    and not any(c.isspace() for c in value)):
                credential = True
        if credential:
            yield number, "credential"


def path_kind(relative):
    path = Path(relative)
    parts = path.parts
    name = path.name
    if (parts[0] in {"data", "sessions", "groups", "backups", "Voices", "models", "logs"}
            or relative in {"config/app.yaml", "config/sessions_meta.json", "config/ui_prefs.json",
                            "startup_progress.json"}
            or (parts[0] == "profiles" and (len(parts) != 2 or name not in EXAMPLES))
            or re.search(r"\.(?:db|sqlite3?)(?:-(?:shm|wal|journal))?$", name)
            or name.endswith(".log")):
        return "runtime-data"
    if name.startswith(".env") and name != ".env.example":
        return "local-config"
    if name in {"AGENTS.md", "CLAUDE.md"} or "superpowers" in parts:
        return "local-instructions"
    if re.fullmatch(r"README(?:_zh)?\.(?:draft|review)\..+", name):
        return "local-review-artifact"
    return None


def git(root, *args, check=True):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if check and result.returncode:
        # Git errors can contain file contents or commit subjects: never echo them.
        raise RuntimeError("Git audit failed during " + args[0])
    return result


def source_files(root):
    def walk_error(_error):
        raise RuntimeError("Source traversal failed")
    for directory, folders, names in os.walk(root, followlinks=False, onerror=walk_error):
        folders[:] = sorted(folder for folder in folders if folder not in SOURCE_SKIP)
        for folder in list(folders):
            path = Path(directory) / folder
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                yield path.relative_to(root).as_posix()
                folders.remove(folder)
        for name in sorted(names):
            yield (Path(directory) / name).relative_to(root).as_posix()


def audit(root):
    in_git = (root / ".git").exists()
    if in_git:
        files = git(root, "ls-files", "-z").stdout.decode("utf-8").split("\0")
        files = sorted(set(filter(None, files)))
    else:
        files = list(source_files(root))
    findings = []
    for relative in files:
        path = root / relative
        # Never follow symlinks/junctions outside the selected source root.
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            findings.append((relative, 1, "unsafe-link"))
            continue
        kind = path_kind(relative)
        if kind:
            findings.append((relative, 1, kind))
            continue
        try:
            with path.open("rb") as handle:
                data = handle.read(8 * 1024 * 1024 + 1)
        except OSError:
            findings.append((relative, 1, "unreadable-file"))
            continue
        if b"\0" in data:
            continue  # Text audit only; binary contents are outside this check.
        if len(data) > 8 * 1024 * 1024:
            findings.append((relative, 1, "oversized-text"))
            continue
        text = data.decode("utf-8-sig", errors="replace")
        findings.extend((relative, line, kind) for line, kind in content_findings(text))
    print(f"File audit: {len(files)} {'tracked' if in_git else 'source'} files (text contents and known local paths).")
    commits = 0
    if in_git:
        if git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0:
            log = git(root, "log", "--format=%h%x00%B%x00", "HEAD", "--not", "--remotes").stdout
            fields = log.decode("utf-8", errors="replace").split("\0")
            for index in range(0, len(fields) - 1, 2):
                sha, body = fields[index].strip(), fields[index + 1]
                commits += 1
                findings.extend(("commit:" + sha, line, kind) for line, kind in content_findings(body))
        print(f"Commit audit: {commits} commits not reachable from local remote-tracking refs (no fetch).")
    else:
        print("Commit audit skipped: no .git in source root.")
    for path, line, kind in findings:
        print(f"{path}:{line}:{kind}")
    print(f"Leak check: {len(findings)} findings. This bounded audit does not prove absence of secrets.")
    return 1 if findings else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        parser.error("source root must be a directory")
    try:
        return audit(root)
    except (OSError, RuntimeError, UnicodeError):
        print("Leak check could not complete; no source text or credential values are printed.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
