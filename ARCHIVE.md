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
local/cloud provider namespace. An unavailable configured archive fails explicitly
rather than presenting an incomplete result as complete.

The sync script persists an installation ID in one-conv's state directory. Keep
that directory across runs. Uploads are content-addressed and compare the expected
remote revision before advancing a conversation. Repeats are idempotent; stale,
concurrent and rewritten/truncated histories are rejected (including compaction that replaces prior turns). A changing source is skipped with
an error and can be retried. Earlier revisions remain stored. No automatic
scheduler, deletion propagation, local eviction or provider resume restoration
is included.

Captures retain native records as base64 plus normalized blocks for existing
renderers. File sources include their captured files; Cursor CLI uses a consistent
SQLite backup and metadata; Cursor IDE captures the conversation's database rows.
External attachments and records missing from provider caches are not guaranteed.
This is not a complete account backfill or a tested replacement for native backups.
Revisions and original records consume more space than the searchable text alone.

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
search text. Benchmark larger archives before adding a database-specific index.

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
