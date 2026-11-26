"""`one-conv board`: post notes and files on shared topics, and read them later. Pull-only, quiet."""

import json
import sys
import time
from pathlib import Path

import click

from . import board

HELP = {"help_option_names": ["-h", "--help"]}


def examples(*lines: str) -> str:
    """A help epilog click prints as written: its leading \\b keeps the line breaks."""
    return "\b\nExamples:\n" + "\n".join(f"  {line}" for line in lines)


def _size(size: int) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1000 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size} B"


def _session():
    from .live import router
    try:
        return router.whoami()
    except router.LiveError:
        return None


def _echo_post(row: dict, topic_hint: bool = False) -> None:
    when = time.strftime("%m-%d %H:%M", time.localtime(row.get("created") or 0))
    who = row["from"] + (f" ({row['name']})" if row.get("name") else "")
    labels = f"  [{', '.join(row['tags'])}]" if row.get("tags") else ""
    where = f"{row['topic']}  " if topic_hint else ""
    click.echo(f"#{row['seq']}  {where}{when}  {who}{labels}  id={row['id']}")
    for line in (row.get("text") or "").splitlines() or ([""] if not row.get("files") else []):
        click.echo(f"  {line}")
    for file in row.get("files", []):
        click.echo(f"  file: {file['name']} ({_size(file['size'])}, {file['content_type']})"
                   f"  one-conv board get {row['topic']} {file['id']}")


def build_board_group() -> click.Group:
    @click.group("board", context_settings=HELP, epilog=examples(
        "one-conv board topics",
        "one-conv board read release-2-0",
        "one-conv board post release-2-0 'Decision: ship behind the flag; rollback is a config flip' --tag decision --yes",
        "one-conv board post pr-42 'Benchmark before/after' --file bench.csv --yes",
        "one-conv board get pr-42 9f2c41d07a5e4b1c8d3e6f7a8b9c0d1e -o bench.csv",
        "one-conv board search 'rollback' --topic release-2-0"))
    def group():
        """Shared notes on named topics, kept on the live relay for every machine to read later.

        The board holds context, decisions, findings and files that others will need later.
        Posting wakes nobody and sends no notification: readers pull with `board topics` and
        `board read`, which remembers what each reader has seen. To ask someone to act now,
        use `one-conv live send` instead. A topic is a slug (lowercase, digits, dashes) named
        after the project or issue; its first post creates it.
        """

    def fail(error: Exception):
        raise click.ClickException(str(error)) from None

    @group.command("topics", context_settings=HELP, epilog=examples("one-conv board topics", "one-conv board topics --json"))
    @click.option("--as", "reader", help="Reader whose unread counts to show; defaults to this session, or user.")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def topics_command(reader, as_json):
        """List topics, most recently posted first, with how many posts each has unread for you."""
        try:
            rows = board.topics(board.reader_for(reader, _session()))
        except board.BoardError as error:
            fail(error)
        if as_json:
            click.echo(json.dumps(rows, ensure_ascii=False))
            return
        if not rows:
            click.echo("No topics yet. Post with `one-conv board post TOPIC TEXT --yes`.", err=True)
            return
        width = max(len(row["topic"]) for row in rows)
        for row in rows:
            when = time.strftime("%m-%d %H:%M", time.localtime(row.get("last_post") or 0))
            title = f"  {row['title']}" if row.get("title") else ""
            click.echo(f"{row['topic']:<{width}}  {row['unread']:>3} unread  {row['posts']:>4} posts  {when}{title}")

    @group.command("read", context_settings=HELP, epilog=examples(
        "one-conv board read release-2-0", "one-conv board read release-2-0 --tag decision",
        "one-conv board read release-2-0 --all --limit 10", "one-conv board read release-2-0 --peek --json"))
    @click.argument("topic")
    @click.option("--all", "show_all", is_flag=True, help="Show the latest posts, read or not, without moving your position.")
    @click.option("--peek", is_flag=True, help="Show unread posts without marking them read.")
    @click.option("--tag", "tags", multiple=True, metavar="TAG", help="Only posts carrying this tag (repeatable: any of them).")
    @click.option("--limit", default=50, show_default=True, help="Most posts to show.")
    @click.option("--as", "reader", help="Reader whose position moves; defaults to this session, or user.")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def read_command(topic, show_all, peek, tags, limit, reader, as_json):
        """Show TOPIC's unread posts, oldest first, and mark them read.

        Each tag filter keeps its own position, so `--tag decision` leaves other posts unread.
        """
        try:
            page = board.read(topic, board.reader_for(reader, _session()), list(tags), limit, peek, show_all)
        except board.BoardError as error:
            fail(error)
        if as_json:
            click.echo(json.dumps(page, ensure_ascii=False))
            return
        if not page["posts"]:
            click.echo(f"No unread posts in {topic}." if not show_all else f"No posts in {topic}.", err=True)
            return
        click.echo(f"# {topic}" + (f": {page['title']}" if page.get("title") else ""))
        for row in page["posts"]:
            _echo_post(row)

    @group.command("post", context_settings=HELP, epilog=examples(
        "one-conv board post release-2-0 'Decision: ship behind the flag' --tag decision --yes",
        "one-conv board post pr-42 'Profile of the slow path' --file profile.svg --file notes.md --yes",
        "one-conv board post infra --stdin --title 'Infra notes' --yes < findings.md"))
    @click.argument("topic")
    @click.argument("text", required=False)
    @click.option("--stdin", "from_stdin", is_flag=True, help="Read the text from stdin.")
    @click.option("--file", "files", multiple=True, metavar="PATH",
                  help=f"Attach a file (repeatable, at most {board.FILES_PER_POST}, {board.FILE_LIMIT // 1_000_000} MB each).")
    @click.option("--tag", "tags", multiple=True, metavar="TAG", help="Label the post (repeatable); `board read --tag` filters by it.")
    @click.option("--title", default="", help="Set the topic's title.")
    @click.option("--yes", is_flag=True, help="Actually post. Without it, prints what would be posted.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the receipt as JSON.")
    def post_command(topic, text, from_stdin, files, tags, title, yes, as_json):
        """Post TEXT (and files) to TOPIC, creating it on its first post. Nobody is notified.

        Post decisions, findings and artifacts others will need, concisely; never progress
        chatter. The author is this session (or user at a terminal). Repeating the same post
        within 10 minutes is stored once.
        """
        from .live import router
        if from_stdin:
            text = sys.stdin.read()
        try:
            prepared = board.plan(topic, text or "", list(tags), list(files))
            session = _session()
            author, name = board.author_for(session, router.origin(sys.stdin.isatty() and not from_stdin))
            if not yes:
                click.echo("DRY-RUN (nothing posted). Re-run with --yes to post.")
                click.echo(f"  topic:    {prepared['topic']}" + (f"  (title: {title})" if title else ""))
                click.echo(f"  from:     {author}" + (f" ({name})" if name else ""))
                click.echo(f"  text:     {len(prepared['text'].encode())} bytes"
                           + (f", tagged {', '.join(prepared['tags'])}" if prepared["tags"] else ""))
                for file in prepared["files"]:
                    click.echo(f"  file:     {file['name']} ({_size(file['size'])}, {file['content_type']})")
                board.client()
                return
            receipt = board.post(prepared, author, name, board.reader_for(None, session), title)
        except board.BoardError as error:
            fail(error)
        if as_json:
            click.echo(json.dumps(receipt))
            return
        click.echo(f"{'already posted' if receipt.get('duplicate') else 'posted'}: {receipt['topic']} #{receipt['seq']}"
                   f"  id={receipt['id']}")

    @group.command("get", context_settings=HELP, epilog=examples(
        "one-conv board get pr-42 9f2c41d07a5e4b1c8d3e6f7a8b9c0d1e",
        "one-conv board get pr-42 9f2c41d07a5e4b1c8d3e6f7a8b9c0d1e -o /tmp/bench.csv --json"))
    @click.argument("topic")
    @click.argument("file_id")
    @click.option("-o", "--output", type=click.Path(dir_okay=False, writable=True),
                  help="Where to write it; defaults to the file's name in the current folder.")
    @click.option("--force", is_flag=True, help="Overwrite an existing file.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the file's metadata and path as JSON.")
    def get_command(topic, file_id, output, force, as_json):
        """Download a file a post on TOPIC carries, checked against its sha256."""
        try:
            meta, data = board.get_file(topic, file_id)
        except board.BoardError as error:
            fail(error)
        path = Path(output) if output else Path.cwd() / meta["name"]
        if path.exists() and not force:
            raise click.ClickException(f"{path} exists; pass --force or -o another path.")
        path.write_bytes(data)
        if as_json:
            click.echo(json.dumps({**meta, "path": str(path)}, ensure_ascii=False))
            return
        click.echo(f"{path}  ({_size(meta['size'])}, sha256 {meta['sha256'][:12]}...)")

    @group.command("search", context_settings=HELP, epilog=examples(
        "one-conv board search rollback", "one-conv board search 'lock migration' --all-words --topic infra --json"))
    @click.argument("query")
    @click.option("--topic", help="Only this topic.")
    @click.option("--all-words", is_flag=True, help="Match posts holding every word, in any order, instead of the exact text.")
    @click.option("--limit", default=20, show_default=True, help="Most posts to show.")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def search_command(query, topic, all_words, limit, as_json):
        """Search post text, newest first (ASCII case ignored). Moves no read position."""
        try:
            rows = board.search(query, topic, all_words, limit)
        except board.BoardError as error:
            fail(error)
        if as_json:
            click.echo(json.dumps(rows, ensure_ascii=False))
            return
        if not rows:
            click.echo("No posts match.", err=True)
        for row in rows:
            _echo_post(row, topic_hint=True)

    @group.command("rm", context_settings=HELP, epilog=examples(
        "one-conv board rm pr-42 9f2c41d07a5e4b1c8d3e6f7a8b9c0d1e --yes"))
    @click.argument("topic")
    @click.argument("post_id")
    @click.option("--yes", is_flag=True, help="Actually delete. Without it, only says what would go.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the result as JSON.")
    def rm_command(topic, post_id, yes, as_json):
        """Hide a post (and its files) from every reader. Only the machine that posted it, or the relay's admin, can."""
        if not yes:
            click.echo(f"DRY-RUN (nothing deleted). Re-run with --yes to delete post {post_id} from {topic}.")
            return
        try:
            result = board.remove(topic, post_id)
        except board.BoardError as error:
            fail(error)
        click.echo(json.dumps(result) if as_json else f"deleted: {topic} {post_id}")

    return group
