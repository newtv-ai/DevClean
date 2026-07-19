from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The strict JSON loaders reject a UTF-8 BOM by design, and the release
# validator's ``ast.parse`` fails on BOM-prefixed wheel sources, so a BOM in
# any tracked text file silently breaks tests, gates, or the release build.
TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".ini",
        ".json",
        ".lock",
        ".md",
        ".ps1",
        ".py",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)


def test_no_repository_text_file_starts_with_a_utf8_bom() -> None:
    listing = subprocess.run(
        ("git", "ls-files", "-c", "-o", "--exclude-standard"),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    offenders = [
        line
        for line in listing.splitlines()
        if (path := ROOT / line).suffix.lower() in TEXT_SUFFIXES
        and path.is_file()
        and path.open("rb").read(3) == b"\xef\xbb\xbf"
    ]
    assert offenders == []
