#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Build the universal credential helper the Claude Desktop extension ships (mcpb.json `build`)."""

import logging
from pathlib import Path
import sys

# The repo root, so the build reuses native_auth's exact compile and signing steps.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from one_conv import native_auth  # noqa: E402

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    native_auth.PREBUILT.parent.mkdir(parents=True, exist_ok=True)
    native_auth.build_helper(native_auth.PREBUILT, universal=True)
    log.info("built %s", native_auth.PREBUILT)


if __name__ == "__main__":
    main()
