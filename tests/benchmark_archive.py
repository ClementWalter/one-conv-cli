"""Opt-in scale benchmark: uv run ... python -m pytest tests/benchmark_archive.py -s.

ONE_CONV_BENCHMARK_GIB defaults to 7 GiB of synthetic searchable/native text.
Uses isolated storage, the production SQLite writer and real HTTP/CLI reads.
"""
import base64
import json
import math
import os
import platform
import random
import sqlite3
import time
from pathlib import Path

from test_archive import archive, fixture_env, run


def test_search_scale(tmp_path, archive):
    store, _, url = archive
    target = float(os.environ.get("ONE_CONV_BENCHMARK_GIB", "7")) * 1024 ** 3
    assert target > 0
    text_bytes = 1024 ** 2
    count = math.ceil(target / text_bytes)
    rng = random.Random(20261013)
    start = time.perf_counter()
    rare_sessions = set()
    for i in range(count):
        session = f"benchmark-{i}"
        # Unique high-entropy text avoids an unrealistically tiny deduplicated DB.
        text = "benchmark common phrase " + rng.randbytes(text_bytes // 2).hex()
        if i % 1000 == 0:
            text += " rare ancestry needle "
            rare_sessions.add(session)
        turn = {"ts": "2026-01-01T10:00:00Z", "role": "user", "blocks": [{"type": "text", "text": text}]}
        document = {"origin": "synthetic-benchmark", "source": "claude", "cwd": "/benchmark",
            "session": session, "mtime": i, "unread_native": None,
            "summary": {"uuid": session, "source": "claude", "cwd": "/benchmark", "start": turn["ts"],
                "end": turn["ts"], "turns": 1, "tokens": None, "title": "Benchmark conversation"},
            "turns": [turn], "turns_with_subagents": None, "search_text": [text],
            "originals": {"synthetic.jsonl": base64.b64encode(json.dumps(turn).encode()).decode()}}
        store.put(document, None)
        if (i + 1) % 1000 == 0:
            print(json.dumps({"populated": i + 1, "seconds": round(time.perf_counter() - start, 2)}), flush=True)
    report = {"platform": platform.platform(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "conversations": count, "searchable_bytes": count * text_bytes,
        "database_bytes": Path(store.engine.url.database).stat().st_size,
        "populate_seconds": round(time.perf_counter() - start, 2), "queries": {}}
    env = fixture_env(tmp_path)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    for query, expected in [("absent benchmark zzz", 0), ("rare ancestry needle", min(len(rare_sessions), 50)),
                            ("benchmark common phrase", min(count, 50))]:
        measurements = []
        for _ in range(3):
            start = time.perf_counter()
            rows = run(env, "bin/one-conv", "local", "search", query, "--source", "claude", "--json")
            measurements.append(round(time.perf_counter() - start, 3))
            assert len(rows) == expected
            assert all(query in row["text"] for row in rows)
            if query == "rare ancestry needle":
                assert {row["session"] for row in rows} <= rare_sessions
        report["queries"][query] = {"seconds": measurements, "matches": expected}
        print(json.dumps({"query": query, **report["queries"][query]}), flush=True)
    output = tmp_path / "benchmark.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Report: {output}", flush=True)
    print(json.dumps(report), flush=True)
