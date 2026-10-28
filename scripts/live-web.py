#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "click==8.3.3",
#     "starlette==1.8.0",
#     "uvicorn==0.54.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Serve the local agent workspace with human messaging: live-web.py --port 8790."""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from one_conv.live.web import application


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger(__name__).info("one-conv live workspace on http://127.0.0.1:%d", args.port)
    # Loopback only: the page shows what agents told each other and has no login.
    uvicorn.run(application(args.port), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
