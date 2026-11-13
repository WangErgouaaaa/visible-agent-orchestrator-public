#!/usr/bin/env python3
"""Check the distributable tree without reading host credentials or live state."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path


PRIVATE_PATH = re.compile(r"/(?:data/(?:sync|agent|software)|home/(?!example(?:/|$)|user(?:/|$))[^/\s\"']+)(?:/|\b)")
SECRET = re.compile(
    r"\b(?:AKIA[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}|sk-(?:proj-|ant-)[A-Za-z0-9_-]{30,})\b"
)
HOST_WORKER = re.compile(r"\b(?:vw-user\d+|vc-user-claude\d+)\b")
HOST_LAYOUT = re.compile(
    r"\b(?:workers?|rebind)\s+\d{4,5}\b|\b\d{4,5}(?:/\d{4,5})+\b|\btouch\s+\d+%\s+panel\b"
)
EXCLUDED = re.compile(r"(?:academic|co[-_]scientist)", re.IGNORECASE)
PRIVATE_DIRS = {".omx", ".planning", ".superpowers", "planning-with-files", "multi-agent_questions", "multi-agent_question_artifacts", "process-journal", "process-documentation"}


def check_file(relative: str, content: str) -> list[str]:
    errors = []
    path = Path(relative)
    if EXCLUDED.search(relative) or PRIVATE_DIRS.intersection(path.parts):
        errors.append("excluded extension or private artifact path")
    for number, line in enumerate(content.splitlines(), 1):
        if PRIVATE_PATH.search(line):
            errors.append(f"line {number}: host-specific absolute path")
        if SECRET.search(line):
            errors.append(f"line {number}: credential-shaped value")
        if HOST_WORKER.search(line) or HOST_LAYOUT.search(line):
            errors.append(f"line {number}: host-specific worker identity or layout")
    if path.suffix == ".py" or (path.parent == Path("bin") and content.startswith("#!/usr/bin/env python")):
        try:
            ast.parse(content, filename=relative)
        except SyntaxError as exc:
            errors.append(f"line {exc.lineno}: invalid Python syntax")
    if path.suffix == ".json" and "tests" not in path.parts:
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            errors.append(f"line {exc.lineno}: invalid JSON")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        assert check_file("src/tool.py", 'ROOT = "' + "/data/" + 'sync"')
        assert check_file("plugins/" + "academic" + "/data.json", "{}")
        assert check_file("src/tool.py", "WORKER = '" + "vw-user" + "9999'")
        assert check_file("src/tool.py", "ACTION = 'restore or rebind " + "9999'")
        assert check_file("src/tool.py", "ACTION = 'use " + "/".join(("9997", "9998", "9999")) + "'")
        assert check_file("src/tool.py", "ACTION = 'touch " + "9% panel'")
        assert check_file("src/tool.py", "def broken(")
        assert check_file("contracts/broken.json", "{")
        assert not check_file("src/tool.py", 'ROOT = "/workspace/example"')
        assert not check_file("src/tool.py", "PORT = 4096")
        assert not check_file("contracts/example.json", '{"endpoint":"https://example.invalid"}')
        print("public-tree checker self-test passed")
        return 0

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True, check=True,
    )
    paths = sorted(set(result.stdout.decode().rstrip("\0").split("\0")))
    errors = []
    for relative in paths:
        if not relative:
            continue
        path = root / relative
        if path.is_symlink():
            errors.append(f"{relative}: symlinks are not part of the public source payload")
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            errors.append(f"{relative}: unreadable or non-text payload requires review")
            continue
        errors.extend(f"{relative}: {error}" for error in check_file(relative, content))
    license_path = root / "LICENSE"
    if not license_path.is_file() or not license_path.read_text().startswith("MIT License\n"):
        errors.append("LICENSE: expected MIT license")
    for error in errors:
        print(error)
    print(json.dumps({"files_checked": len(paths), "errors": len(errors)}))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
