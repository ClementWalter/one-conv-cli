FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58
WORKDIR /app
COPY scripts/archive-server.py scripts/archive-server.py
RUN uv sync --script scripts/archive-server.py
COPY archive archive
COPY one_conv/archive.py one_conv/archive.py
ENV HOST=0.0.0.0
CMD ["uv", "run", "--script", "scripts/archive-server.py"]
