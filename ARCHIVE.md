# Optional conversation archive

## The live controller's archive

The cloud controller (`one-conv live control serve`) serves an archive at
`/archive` on its public origin, authenticated with the node tokens its hub
issues. Every node token reads every capture; a node uploads only captures whose
origin is its own node name.

A machine running `one-conv live serve` needs nothing else. The bridge runs
`scripts/archive-sync.py` in the background for Claude Code, Codex, Oh My Pi and
Cursor history, every 5 minutes (`ONE_CONV_ARCHIVE_SYNC_MINUTES`; `0` turns it
off), with its hub URL and node token, and uploads under its node name. Its
output goes to `archive-sync.log` in the bridge's state directory. Readers on
that machine use the same settings, so `local chats`, `search`, `find`, `read`
and `thread` include every node's sessions. `ONE_CONV_ARCHIVE_URL` set to
another archive takes precedence; set to an empty value, it turns the archive
off.

A live address names a session on every machine:

```sh
one-conv local thread worker-1/codex:019a... --no-mark-read --json
one-conv local thread macbook/claude:2d69d3d1-... --around 2026-10-19T14:05:00Z --context 3
one-conv local thread macbook/claude:2d69d3d1-... --around MESSAGE_ID
```

`NODE` is this machine's node name: native history is read, and the archive
copy is used when the native files are gone. Any other `NODE` is read from the
archive. A bare `harness:id` names this machine. `search` and `find` JSON rows
carry each agent session's `address`. The MCP `read_conversation` tool takes the
address as `query`, with `around` and `context`.

The controller needs `sqlalchemy` and `pydantic`, which its installer pins;
without them it logs that the archive is disabled and serves the hub alone.

## A standalone archive

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
- A conversation is re-read when its main file's modification time, the one-conv
  version that captured it, or its source stamp differs from the last capture, or
  when the archive's current revision is no longer the one this machine uploaded or
  confirmed. The source stamp covers every file a conversation reads: the files in a
  Claude session folder, a Cursor CLI store's write-ahead log, and, for Cursor IDE,
  that conversation's own database rows. Stamps live in `archive-stamps.json` in the
  state directory.
- Inside a Claude session folder, a file or subfolder that is already a symlink is
  skipped, and one replaced by a symlink during capture makes the read refuse. This
  covers the session folder only.
- A conversation's own files outside that folder (its transcript, a Cursor CLI
  store and its `meta.json`) must be regular files: one that is a symlink, before
  or during capture, makes that conversation fail instead of archiving another file.
- For Claude Code, Codex, Oh My Pi and Cursor CLI, each file is read once without
  following symlinks, and the turns, title and token count are parsed from a
  private copy of those bytes, so everything archived comes from the same read. A
  system without no-follow file opens cannot archive these sources.
- The session id, working directory and title shown in listings are read again
  from that copy. If the session id or working directory no longer matches the
  listing, or the copy does not hold the directory, that conversation fails and
  is retried on the next sync. A Claude Code project directory is accepted when it
  matches the project folder's name or appears in the captured transcript.
- Each native record is archived separately, even when records share a session ID
  (Codex subagent rollouts share their parent's).
- Each upload names the revision it replaces, so concurrent captures are rejected.
  An installation's current native record always becomes current, including a
  rewrite of earlier turns (provider edit or regeneration, a parser upgrade) or a
  file restored from an older backup; earlier revisions stay readable by revision.
- A failed conversation is reported on stderr; sync continues and exits nonzero with
  uploaded, unchanged and failed counts. Retry a changing source when it is idle.

## Automatic sync

With an archive configured, one-conv commands start a sync in the background when
the last one started more than 15 minutes ago, and return without waiting. Nothing
is installed on the machine: sync runs only while one-conv is used.
`ONE_CONV_ARCHIVE_SYNC_MINUTES` changes the interval; `0` turns automatic sync off.
Automatic sync covers every native source; `corpus` rows are imported copies with no native files.

Each sync records its result in one-conv's state directory and appends to
`archive-sync.log` there; these files are readable only by their owner. While the
last sync has failures, or automatic sync is on and none has completed for a day,
every command prints a warning on stderr. Only one sync runs at a time; a sync that
stopped without finishing blocks others for at most an hour.

## Freeing local space

`uv run scripts/archive-sync.py --source claude --source codex --source omp --free-older-than 90`
syncs, then deletes Claude Code, Codex and Oh My Pi conversations unchanged for 90
days whose every file the archive holds byte for byte. A conversation with any file
the archive lacks, that changes during the check, whose session is running (listed by
`one-conv live` or held open by a process), or whose folders cannot be opened without
following symlinks, is kept, and the reason is printed. Deletion goes through folders
opened once at the start, so a folder replaced during the check cannot redirect it. Cursor conversations live in shared databases and are never deleted. Freed
conversations still appear in every one-conv command, read from the archive.

Before deleting, freeing marks the archived capture as freed from this machine.
`uv run scripts/archive-sync.py --restore SESSION` writes back each of the
conversation's records that was freed or is missing here (a Codex session also has
one per subagent), from the newest copy this machine can hold, then clears the
mark. It writes only inside this machine's Claude Code, Codex and Oh My Pi history
folders and never over a different file.

If a tool writes to a conversation after it was freed here, the new file holds only
what was written since. Sync does not upload it over the archived conversation, even
after the state directory is reset; it reports the file and the steps to recover:
move the file away, restore the conversation, then add back what the moved file
holds.

If the archive is unreachable, commands list archived conversations from the last
successful listing and warn; opening one that exists only in the archive fails
until the archive is reachable again. There is no deletion propagation between
machines.

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

## Incremental metadata listing

`GET /v1/conversations?since=0` lists all heads in change-sequence order. Each
page returns `items` in the usual metadata shape, `next` (an integer cursor or
`null`), `sequence` (the read snapshot's watermark), and `generation` (the
database's persistent UUID). Request `since=next` until `next` is `null`, merge
items by key, and save the final page's `sequence` for the next refresh. Repeated
keys are replaced: a conversation can change between pages.

Keep `generation` with the cached rows. If it changes on any page, discard the
cache and restart from `since=0`; a replaced database has a new sequence space.
Only publish the merged cache after every page succeeds. A server without these
fields requires the legacy full listing. `since` cannot combine with `after` or
`query`; requests without `since` retain key-based pagination and substring search.

Successful head changes advance the sequence atomically; identical revisions
and failed commits do not. Archived heads have no deletion API. Any future
deletion API must expose tombstones or invalidate the database generation.
Existing SQLite databases receive baseline sequences automatically on startup.

## Add a backend

Implement `ConversationStore` in `archive/store.py`: paginated metadata listing
with an optional literal substring filter over search text, sequence-based delta
listing with a persistent generation, manifest reads, chunk
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
