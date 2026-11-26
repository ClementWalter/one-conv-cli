#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Print the next Desktop extension version: the highest vX.Y.Z tag with its patch bumped.

Reads tag names on stdin (`git tag -l 'v*' | scripts/next-version.py`). Versions compare as
integer tuples, like the update check (one_conv/update_check.py), so 0.1.10 follows 0.1.9.
Tags that are not plain vX.Y.Z (pre-releases, other names) are ignored.
"""

import re
import sys

SEMVER = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def next_version(tags: list[str]) -> str:
    versions = [tuple(map(int, m.groups())) for t in tags if (m := SEMVER.match(t.strip()))]
    major, minor, patch = max(versions, default=(0, 1, -1))
    return f"{major}.{minor}.{patch + 1}"


if __name__ == "__main__":
    sys.stdout.write(next_version(sys.stdin.read().split()) + "\n")
