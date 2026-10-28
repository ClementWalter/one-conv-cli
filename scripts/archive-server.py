#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "sqlalchemy==2.1.4",
#     "starlette==1.8.0",
#     "pydantic==2.12.5",
#     "uvicorn==0.54.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn

from archive.server import application

if __name__ == "__main__":
    os.umask(0o077)
    uvicorn.run(application(), access_log=False, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
