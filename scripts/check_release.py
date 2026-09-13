#!/usr/bin/env python3
"""Check the Git index for private data, recognizable secrets and personal paths.

Reads staged blobs, not local data. Reports filenames and line numbers only.
This is a project guardrail, not a complete secret scanner.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
# Only visually reviewed, synthetic demo assets may enter a release. Re-recording
# requires reviewing the new frames and updating the exact content digest here.
# Other binary files, including screenshots of private sessions, remain blocked.
REVIEWED_MEDIA = {
    "docs/assets/learning-tree-overview.png": (
        "c9ec9b8694ce6bff87a69e6a445cf9152f33acc3c1c7ca34fb502955a2d1baaf"
    ),
    "docs/assets/learning-tree-demo.gif": (
        "4cef37b63207ca010f0d7d2deccae4bec963b1d0b02986dc6b5b09d2834035f4"
    ),
}
PRIVATE_DIRS = {
    ".backups",
    ".runtime",
    ".local",
    ".venv",
    "mcp-data",
    "artifacts",
    "node_modules",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "provider token": re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{24,}"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "personal home path": re.compile(r"(?:/" r"Users/[^/\s]+/|C:\\" r"Users\\[^\\\s]+\\)"),
    "credential URL": re.compile(r"https?://[^\s/:]+:[^\s/@]+@"),
}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main() -> int:
    errors: list[str] = []
    entries = git("ls-files", "--stage", "-z").split(b"\0")
    count = 0
    for entry in filter(None, entries):
        metadata, encoded_path = entry.split(b"\t", 1)
        mode, oid, stage = metadata.decode().split()
        name = encoded_path.decode()
        path = PurePosixPath(name)
        count += 1
        if stage != "0" or mode not in {"100644", "100755"}:
            errors.append(f"{name}: unresolved or non-regular source entry")
            continue
        forbidden = (
            any(part in PRIVATE_DIRS for part in path.parts)
            or (path.name.startswith(".env") and path.name != ".env.example")
            or bool(re.search(r"\.(?:db|sqlite3?)(?:$|-)|\.(?:pem|key|p12)$", path.name))
            or (path.parts[0] == "学习资料" and name != "学习资料/使用说明.md")
            or path.name.startswith(("优化交付记录", "更新记录"))
        )
        if forbidden:
            errors.append(f"{name}: private/generated file must not be published")
            continue
        content = git("cat-file", "blob", oid)
        if content.startswith(b"SQLite format 3"):
            errors.append(f"{name}: SQLite data must not be published")
            continue
        if name in REVIEWED_MEDIA:
            if hashlib.sha256(content).hexdigest() != REVIEWED_MEDIA[name]:
                errors.append(f"{name}: media changed; explicit visual/privacy review required")
            continue
        try:
            source = content.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"{name}: binary content requires explicit release review")
            continue
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(source):
                line = source.count("\n", 0, match.start()) + 1
                errors.append(f"{name}:{line}: {label}")
    if not count:
        errors.append("No staged source files found. Stage the intended release first.")
    if errors:
        print("Release check failed (matched values are not printed):")
        print("\n".join(errors))
        return 1
    print(f"Release check passed: {count} staged source files; no flagged private content.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
