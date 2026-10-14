# Optional conversation archive

Copy captured history to your own service and use the existing `chats`, `find`,
`search`, `read`, and `thread` commands. Provider filters and JSON shapes stay the
same. Nothing deletes or changes native history. Python commands use `uv`.

## Run locally

Generate a private token and keep it out of shell history, source control and logs.
Set it as `ONE_CONV_ARCHIVE_TOKEN` for both the server and client.

```sh
export ONE_CONV_ARCHIVE_DATABASE_URL=sqlite:////absolute/path/archive.db
uv run scripts/archive-server.py
```

The database's parent directory must exist. The default listener is
`127.0.0.1:8000`; `HOST` and `PORT` configure it. In another terminal:

```sh
export ONE_CONV_ARCHIVE_URL=http://127.0.0.1:8000
uv run scripts/archive-sync.py --source claude --source codex --limit 10
one-conv local search "your query" --json
one-conv local thread PROJECT --source claude --no-mark-read --json
```

`--source` is required and repeatable. `--project` restricts matching project paths;
`--limit 0` (default) copies all discovered conversations in scope. Start bounded.
Cloud sources copy already pulled provider caches; they do not log in or pull.
Configure the URL/token in the environment running one-conv, including its MCP
server. Unset the URL for native-only reads. Archives remain in their original
local/cloud provider namespace. If the configured archive is unreachable or rejects
the token, commands warn on stderr and show native history only.

The sync script persists an installation ID in one-conv's state directory. If the
same conversation is uploaded by several installations (another machine, or a reset
state directory), readers show the newest copy. Sync skips conversations whose
source modification time matches the archive. Uploads are content-addressed and
compare the expected remote revision before advancing a conversation. Repeats are
idempotent; stale and concurrent captures are rejected. A capture that rewrites
earlier turns (a provider edit or regeneration, or a parser upgrade) becomes the
current version; earlier revisions remain stored and readable by revision. Failed
captures are reported individually; sync continues with the remaining conversations
and exits nonzero with uploaded/unchanged/failed counts. A changing source can be
retried when idle. No automatic
scheduler, deletion propagation, local eviction or provider resume restoration
is included.

Captures retain native records as base64 plus normalized blocks for existing
renderers. File sources include their captured files; Cursor CLI uses a consistent
SQLite backup and metadata; Cursor IDE captures the conversation's database rows.
External attachments and records missing from provider caches are not guaranteed.
This is not a complete account backfill or a tested replacement for native backups.
Revisions share content-addressed 64 KiB chunks, independently for each large
field and original record. Appends reuse unchanged chunks while preserving exact
earlier captures. Existing full-document revisions remain readable. Original
records, normalized turns and search text still consume more space than native
history alone; rewriting a source file can reduce chunk reuse.

## Deploy on Railway or another host

Build `archive/Dockerfile` from the repository root. Mount persistent storage,
set `ONE_CONV_ARCHIVE_DATABASE_URL` to a SQLite file on it, and set a private
`ONE_CONV_ARCHIVE_TOKEN`. Expose HTTPS; unencrypted remote HTTP is rejected.
The container listens on `HOST=0.0.0.0` and `PORT` (default 8000).

On Railway, use the current CLI or the `use-railway` skill to select the project,
create a service and volume, and configure those values. Set the documented
`RAILWAY_DOCKERFILE_PATH=archive/Dockerfile` to select the image definition.
Discover identifiers and the generated HTTPS domain from CLI output. No Railway
SDK, project ID or hostname belongs in the runtime. Verify deployment success,
authenticated reads, and persistence after redeployment. Configure volume backups
and verify recovery separately before treating the service as durable storage.

The service is single-user: possession of its token permits reading and uploading
all captures. It does not offer end-to-end encryption or multi-tenant isolation.
Use one SQLite service instance. `ONE_CONV_ARCHIVE_MAX_UPLOAD_BYTES` defaults to
64 MiB per capture to bound request memory; larger captures fail explicitly.
Search uses literal case-insensitive substring matching; it currently scans stored
search text. The scale benchmark below measures this path before introducing an index.

## Add a backend

Implement `ConversationStore` in `archive/store.py`: paginated metadata discovery
(with an optional literal substring filter), conversation reads, and atomic,
idempotent writes with an expected revision. Keep native provider identity,
ordering, raw blocks and old complete captures intact. Configure
`ONE_CONV_ARCHIVE_STORE_FACTORY=your_package.module:factory`; the factory receives
`ONE_CONV_ARCHIVE_DATABASE_URL` and returns the store. Install the package in your
service image. The default factory is `archive.store:SQLiteStore`.

The HTTP `/v1` contract is independent of SQL and hosting. SQL implementations
can reuse SQLAlchemy Core's dialects, but must verify their own transactions,
upserts and search behavior. Only SQLite is supported by this implementation;
PostgreSQL/PlanetScale support must pass the same behavior tests before being
advertised. No CLI or reader changes are required for a new server-side backend.

## Verify

```sh
uv run --with sqlalchemy==2.1.4 --with starlette==1.8.0 --with pydantic==2.12.5 \
  --with uvicorn==0.54.0 --with pytest --with click==8.3.3 \
  python -m pytest tests/test_archive.py -q
```

These tests run a real HTTP service and subprocess CLI/sync commands against
isolated native fixtures. They check hand-written expected content and compare
native output with archive-only output, alongside retries, updates, authentication,
interrupted uploads and persistence. They never remove your native conversations.

### Scale benchmark

This optional benchmark generates synthetic data; it never reads native history.
Run the same uv command above with `tests/benchmark_archive.py -q -s` instead of
`tests/test_archive.py -q`. It defaults to 7 GiB of searchable text across 7,168
conversations, also retaining the original records; allow about 32 GiB of free
space for the database. Set `ONE_CONV_BENCHMARK_GIB=0.01` for a quick smoke run.
It measures the unchanged CLI over loopback HTTP for absent, rare and common
queries, checks expected matches, and writes `benchmark.json` in pytest's
reported temporary directory. Three successive timings are reported; these are
not guaranteed cold-cache measurements or remote-host latency estimates.

Measured on 2026-10-13 on macOS arm64, Python 3.14.2 and SQLite 3.51.2:
7 GiB searchable text, 7,168 conversations, 30.6 GiB database including originals
and normalized captures. Population took 214 seconds. Each row below is three
successive end-to-end CLI runs, with the default 50-result limit.

| Query | Matches | First run | Second run | Third run |
| --- | ---: | ---: | ---: | ---: |
| Absent phrase | 0 | 19.939 s | 13.076 s | 9.419 s |
| Rare phrase | 8 | 3.805 s | 3.889 s | 4.096 s |
| Common phrase | 50 | 4.833 s | 4.564 s | 3.553 s |

This is synthetic high-entropy text over local HTTP, not a measurement of a
user's corpus or Railway. The generated dataset has 7 GiB of searchable content,
which is not equivalent to 7 GiB of provider files containing metadata and other
records. Full substring scans remain a bottleneck; this implementation does not
promise interactive search latency at that scale. Matching captures also transfer
original records, so remote network latency and bandwidth can add cost.

### Local and archived trace views

Archived reads use the existing local renderer: normal reads show prose, `--raw`
includes available thinking and tool details with the same summaries and limits,
and search still matches prose. The archive adds no separate trace mode or export
command. Captures retain normalized blocks and original records even when those
records contain fields or events the local parser does not display. For cloud
sources, the original record is the downloaded cache, not a guarantee of all
provider-side events or attachments.
