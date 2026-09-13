"""Publishing only accepts the exact synthetic media that was reviewed."""

import hashlib
import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("docs/assets/demo.png", b"\x89PNG\r\n\x1a\nsynthetic", 0),
        ("docs/assets/demo.png", b"\x89PNG\r\n\x1a\nchanged", 1),
        ("docs/assets/unreviewed.png", b"\x89PNG\r\n\x1a\nsynthetic", 1),
        ("artifacts/demo.png", b"\x89PNG\r\n\x1a\nsynthetic", 1),
        ("docs/assets/demo.png", b"SQLite format 3\0private", 1),
    ],
)
def test_release_rejects_unreviewed_or_changed_media(monkeypatch, name, content, expected):
    script = Path(__file__).resolve().parents[1] / "scripts/check_release.py"
    spec = importlib.util.spec_from_file_location("release_check", script)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    approved = hashlib.sha256(b"\x89PNG\r\n\x1a\nsynthetic").hexdigest()
    monkeypatch.setattr(checker, "REVIEWED_MEDIA", {"docs/assets/demo.png": approved})

    def git(*args):
        if args[0] == "ls-files":
            return f"100644 {'a' * 40} 0\t{name}\0".encode()
        assert args[:2] == ("cat-file", "blob")
        return content

    monkeypatch.setattr(checker, "git", git)
    assert checker.main() == expected
