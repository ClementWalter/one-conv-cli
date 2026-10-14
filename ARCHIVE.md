# Optional conversation archive

Copy captured history to your own service and use the existing `chats`, `find`,
`search`, `read`, and `thread` commands. Provider filters and JSON shapes stay the
same, and archived conversations render exactly as native ones, including `--raw`.
Nothing deletes or changes native history. Python commands use `uv`.

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
Configure the URL and token in the environment running one-conv, including its MCP
server; unset the URL for native-only reads. Archived conversations stay in their
provider's local or cloud namespace. If the archive is unreachable or rejects the
token, commands warn on stderr and show native history only.

## Sync behavior

- The sync script keeps an installation ID in one-conv's state directory. When
  several installations upload one conversation (another machine, or a reset state
  directory), readers show the newest copy.
- A conversation is re-read only when its main source file's modification time
  differs from the archived capture.
- Each upload names the revision it replaces, so stale and concurrent captures are
  rejected. A capture that rewrites earlier turns (provider edit or regeneration, a
  parser upgrade) becomes current; earlier revisions stay readable by revision.
- A failed conversation is reported on stderr; sync continues and exits nonzero with
  uploaded, unchanged and failed counts. Retry a changing source when it is idle.
- There is no scheduler, deletion propagation, local eviction or provider resume.

## What is stored

A capture holds the normalized turns that existing renderers read, the search text,
and the original records: captured files for file sources, a consistent SQLite backup
for Cursor CLI, and the conversation's database rows for Cursor IDE. External
attachments and records missing from provider caches are not included. This is not
a complete account backfill or a tested replacement for native backups.

Original records keep whatever agents printed, including secrets. Possession of the
archive token permits reading and uploading every capture; the service offers no
end-to-end encryption or multi-tenant isolation.

Captures travel and are stored as zlib-compressed, content-addressed 1 MiB chunks,
separately for each field and original record. Requests stay under 16 MiB, so
captures of any size upload in batches, and an append transfers only the changed
tail chunks. Reads fetch normalized turns, never original records.

## Deploy on Railway or another host

Build `archive/Dockerfile` from the repository root. Mount persistent storage, set
`ONE_CONV_ARCHIVE_DATABASE_URL` to a SQLite file on it, and set a private
`ONE_CONV_ARCHIVE_TOKEN`. Expose HTTPS; unencrypted remote HTTP is rejected. The
container listens on `HOST=0.0.0.0` and `PORT` (default 8000). Use one service
instance per SQLite file.

On Railway, use the current CLI or the `use-railway` skill to create a service and
volume and set those values, plus `RAILWAY_DOCKERFILE_PATH=archive/Dockerfile`. No
Railway SDK, project ID or hostname belongs in the runtime. Verify the deployment,
authenticated reads and persistence after redeployment, and configure volume backups
before treating the service as durable storage.

## Add a backend

Implement `ConversationStore` in `archive/store.py`: paginated metadata listing
with an optional literal substring filter over search text, manifest reads, chunk
inventory, storage and reads, and an atomic commit that advances a conversation only
from its expected revision. The server validates captures and derives keys,
revisions and search text. Configure
`ONE_CONV_ARCHIVE_STORE_FACTORY=your_package.module:factory`; the factory receives
`ONE_CONV_ARCHIVE_DATABASE_URL`. The `/v1` HTTP contract and the client do not
change. Only SQLite is verified; another engine must pass the same tests first.

## Verify

```sh
uv run --with-requirements scripts/archive-server.py --with-requirements scripts/archive-sync.py \
  --with pytest python -m pytest tests/test_archive.py -q
```

`tests/benchmark_archive.py` (run with `-s` instead of `tests/test_archive.py`)
measures search over synthetic captures; `ONE_CONV_BENCHMARK_GIB` sets the amount of
searchable text (default 0.25).
