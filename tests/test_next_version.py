#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
#     "pytest-xdist==3.8.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""`scripts/next-version.py` as the release workflow runs it: tag names in, next version out."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "next-version.py"


def next_version(tags: str) -> str:
    return subprocess.run([sys.executable, str(SCRIPT)], input=tags, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.mark.parametrize(("tags", "expected"), [
    ("v0.1.0\n", "0.1.1"),
    ("v0.1.9\nv0.1.10\n", "0.1.11"),
    ("v0.1.10\nv0.1.9\n", "0.1.11"),
    ("v0.2.0\nv0.1.7\n", "0.2.1"),
    ("v1.0.0\nv0.9.9\n", "1.0.1"),
    ("v0.1.3\nv0.2.0-rc1\nlatest\n", "0.1.4"),
    ("", "0.1.0"),
], ids=["patch", "numeric-not-lexical", "unsorted-input", "minor-line", "major-line", "ignores-non-semver", "first"])
def test_next_version(tags, expected):
    assert next_version(tags) == expected
